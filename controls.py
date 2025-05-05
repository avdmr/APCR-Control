#controls.py

import time
import threading
import struct
import socket
import queue
import presets
import logging
import os

REPEAT_INTERVAL = 0.2  # Interval voor herhaling van commando's
IDLE_DELAY = 0.1       # Delay voor het opnieuw versturen van idle-pakketjes
BUFFER_SIZE = 1024

movement_state = {
    'pan':  {'active': False, 'direction': None, 'percentage': 0, 'timer': None, 'idle_timer': None},
    'tilt': {'active': False, 'direction': None, 'percentage': 0, 'timer': None, 'idle_timer': None},
    'roll': {'active': False, 'direction': None, 'percentage': 0, 'timer': None, 'idle_timer': None},
    'zoom': {'active': False, 'direction': None, 'percentage': 0, 'timer': None, 'idle_timer': None}
}

# Globale variabelen voor huidige positie per camid
current_position = {}
last_fdb_timestamp = {}
position_lock = threading.Lock()

_udp_listener_instance = None  
position_queue = queue.Queue()
last_input_time = None

_active_track_monitor_thread = None
_active_track_stop_event = None

virtual_wall_tracker = {
    "pan": None,
    "tilt": None
}

_debug_mode = False

def set_debug_mode(mode):
    """
    Stelt de debug modus in voor de controls module.
    Deze functie wordt aangeroepen vanuit main.py wanneer debug mode wordt in- of uitgeschakeld.
    """
    global _debug_mode
    _debug_mode = mode
    logging.info(f"Debug mode gezet op: {mode}")

def is_debug_mode():
    """
    Controleert of debug modus actief is.
    Gebruikt de lokale _debug_mode variabele die wordt bijgewerkt door set_debug_mode.
    """
    global _debug_mode
    return _debug_mode

def user_input_received():
    global last_input_time
    last_input_time = time.time()

def check_virtual_wall_during_position_update(axis, apcr, current_pos, wall_start, wall_end):
    """
    Wordt periodiek aangeroepen (bij positie-update) voor de geselecteerde camID 
    zolang de laatste input binnen 2 seconden was.
    """
    if last_input_time is None or (time.time() - last_input_time) > 2.0:
        # Geen actieve invoer; reset de tracker (als we buiten de zone zijn)
        virtual_wall_tracker[axis] = None
        return

    # In dit voorbeeld nemen we aan dat we de richting van het laatst ontvangen commando 
    # ergens al hebben opgeslagen als 'last_command_delta' (voor de betreffende as).
    # Dit delta geeft aan in welke richting de gebruiker wilde bewegen.
    global last_command_delta
    if last_command_delta is None:
        return

    # Roep de update functie aan met de huidige positie en de laatst ontvangen delta
    block, trigger_correction = update_virtual_wall_state(axis, apcr, current_pos, last_command_delta, wall_start, wall_end)
    if block:
        print(f"[VirtualWall Update] {axis} input wordt nog geblokkeerd op basis van actuele positie.")
    else:
        print(f"[VirtualWall Update] {axis} input is vrij; tracker gereset.")


def angle_distance(a, b):
    """Bereken de minimale circulaire afstand tussen hoeken a en b."""
    diff = abs(a - b)
    return min(diff, 360 - diff)

#def update_virtual_wall_state(axis, current_pos, command_delta, wall_start, wall_end, virtualwall_active=True, margin=0):
    """
    Bepaalt of een nieuw stuurcommando (voor de opgegeven as) in het virtual wall‑gebied gaat.
    
    Wanneer de as al in het muurgebied zit, wordt er alleen beweging toegestaan als
    deze in de exit‐richting is (dus de tegenovergestelde richting van de beweging waarmee de muur werd betreden).
    Als dat niet zo is, dan wordt een correctie-delta berekend zodat de as automatisch naar de
    virtuele muurgrens (plus 1° buiten) beweegt.
    
    Retourneert een tuple: (block, trigger_correction, correction_delta)
      - block (bool): True als het originele commando geblokkeerd moet worden.
      - trigger_correction (bool): True als dit de eerste keer is dat er correctie nodig is.
      - correction_delta (float): De (automatische) correctiebeweging in graden.
    """
    if not virtualwall_active:
        return False, False, 0

    expected_pos = current_pos + command_delta
    use_absolute = (wall_start < 0 or wall_start >= 360 or wall_end < 0 or wall_end >= 360)
    tracker = virtual_wall_tracker.get(axis, {})

    if use_absolute:
        logging.debug(f"[VirtualWall-ABS] Axis: {axis}, current: {current_pos:.2f}°, delta: {command_delta:.2f}°, expected: {expected_pos:.2f}°")
        logging.debug(f"[VirtualWall-ABS] Wall range (absolute): {wall_start:.2f}° - {wall_end:.2f}°; marge: {margin}°")
        if wall_start - margin <= expected_pos <= wall_end + margin:
            current_in_wall = (wall_start - margin) <= current_pos <= (wall_end + margin)
            if current_in_wall:
                if 'entry_direction' not in tracker:
                    tracker['entry_direction'] = 1 if command_delta > 0 else -1
                    tracker['correcting'] = False
                    virtual_wall_tracker[axis] = tracker
                entry_direction = tracker['entry_direction']
                new_cmd_direction = 1 if command_delta > 0 else -1 if command_delta < 0 else 0
                logging.debug(f"[VirtualWall-ABS] Axis {axis}: entry_direction={entry_direction}, new_cmd_direction={new_cmd_direction}")
                # Als de gebruiker beweegt in de exit-richting, laat dat toe (geen correctie)
                if new_cmd_direction == -entry_direction:
                    return False, False, 0
                else:
                    # Bepaal de exit-target: dit is de grens die je hebt gepasseerd, plus 1° buiten.
                    if entry_direction > 0:
                        # Als je met een positieve beweging binnenkwam, dan was je via de 'onderste' grens binnen.
                        allowed_exit_target = wall_start - 1
                    else:
                        allowed_exit_target = wall_end + 1
                    correction_delta = (allowed_exit_target - current_pos + 540) % 360 - 180
                    logging.info(f"[CORRECTIE-ABS] Axis {axis}: current={current_pos:.2f}°, target={allowed_exit_target:.2f}°, correction_delta={correction_delta:.2f}° (entry_direction={entry_direction})")
                    return True, not tracker.get('correcting', False), correction_delta
            else:
                tracker['entry_direction'] = 1 if command_delta > 0 else -1
                tracker['active'] = True
                tracker['correcting'] = False
                virtual_wall_tracker[axis] = tracker
                return True, True, 0
        else:
            virtual_wall_tracker[axis] = {}
            return False, False, 0
    else:
        norm_current = current_pos % 360.0
        norm_expected = expected_pos % 360.0
        norm_wall_start = wall_start % 360.0
        norm_wall_end = wall_end % 360.0
        logging.debug(f"[VirtualWall-NORM] Axis: {axis}, current (norm): {norm_current:.2f}°, delta: {command_delta:.2f}°, expected (norm): {norm_expected:.2f}°")
        logging.debug(f"[VirtualWall-NORM] Wall range: {norm_wall_start:.2f}° - {norm_wall_end:.2f}°; marge: {margin}°")
        if norm_wall_start <= norm_wall_end:
            wall_contains = (norm_wall_start - margin) <= norm_expected <= (norm_wall_end + margin)
            current_in_wall = (norm_wall_start - margin) <= norm_current <= (norm_wall_end + margin)
        else:
            wall_contains = (norm_expected >= (norm_wall_start - margin)) or (norm_expected <= (norm_wall_end + margin))
            current_in_wall = (norm_current >= (norm_wall_start - margin)) or (norm_current <= (norm_wall_end + margin))
        if wall_contains:
            if current_in_wall:
                if 'entry_direction' not in tracker:
                    tracker['entry_direction'] = 1 if command_delta > 0 else -1
                    tracker['correcting'] = False
                    virtual_wall_tracker[axis] = tracker
                entry_direction = tracker['entry_direction']
                new_cmd_direction = 1 if command_delta > 0 else -1 if command_delta < 0 else 0
                logging.debug(f"[VirtualWall-NORM] Axis {axis}: entry_direction={entry_direction}, new_cmd_direction={new_cmd_direction}")
                if new_cmd_direction == -entry_direction:
                    logging.debug("[VirtualWall-NORM] Commando in exit richting toegestaan.")
                    return False, False, 0
                else:
                    if entry_direction > 0:
                        allowed_exit_target = (norm_wall_start - 1) % 360
                    else:
                        allowed_exit_target = (norm_wall_end + 1) % 360
                    correction_delta = (allowed_exit_target - norm_current + 540) % 360 - 180
                    logging.info(f"[CORRECTIE-NORM] Axis {axis}: current={norm_current:.2f}°, target={allowed_exit_target:.2f}°, correction_delta={correction_delta:.2f}° (entry_direction={entry_direction})")
                    return True, not tracker.get('correcting', False), correction_delta
            else:
                tracker['entry_direction'] = 1 if command_delta > 0 else -1
                tracker['active'] = True
                tracker['correcting'] = False
                virtual_wall_tracker[axis] = tracker
                return True, True, 0
        else:
            virtual_wall_tracker[axis] = {}
            return False, False, 0


def parse_fdb_message_to_dict(msg, ip_addr=None, settings=None):
    """
    Parse een FDB-bericht (bijv. "FDB;1;-161;3;-2;3428;") en retourneer een dictionary met posities.
    
    Als ip_addr en settings zijn opgegeven, wordt het juiste camID bepaald op basis van het IP adres.
    Anders wordt het camID uit het bericht gebruikt.
    """
    parts = msg.strip().split(';')
    if len(parts) >= 6 and parts[0] == "FDB":
        try:
            # Bepaal het juiste camID
            if ip_addr and settings:
                # Zoek het juiste apcr op basis van IP adres
                real_camid = None
                for apcr in settings.get("apcrs", []):
                    if apcr.get("ip") == ip_addr:
                        real_camid = apcr.get("camid")
                        break
                
                if real_camid is None:
                    # Geen match gevonden, gebruik het camID uit het bericht
                    real_camid = int(parts[1])
            else:
                # Geen IP of settings opgegeven, gebruik het camID uit het bericht
                real_camid = int(parts[1])
            
            # Maak position dictionary met het juiste camID
            return {
                'camid': real_camid,
                'pan': float(parts[2]),
                'tilt': float(parts[3]),
                'roll': float(parts[4]),
                'zoom': float(parts[5])
            }
        except Exception as e:
            logging.error(f"Failed to parse FDB message: {e}")
            return None
    return None


def parse_fdb_message(ascii_data, apcr):
    """
    Parse een FDB bericht en sla de positiedata op in de current_position dictionary.
    We gebruiken parts[2..5] voor pan, tilt, roll, zoom.
    Het camID wordt uit de apcr parameter gehaald in plaats van uit het bericht.
    """
    parts = ascii_data.strip().split(';')
    if len(parts) >= 6 and parts[0] == "FDB":
        try:
            # parts[2] = pan, parts[3] = tilt, parts[4] = roll, parts[5] = zoom
            pan_val = float(parts[2])
            tilt_val = float(parts[3])
            roll_val = float(parts[4])
            zoom_val = float(parts[5])
            
            # We slaan het op onder apcr["camid"], NIET het camID uit het bericht
            real_camid = apcr["camid"]
            with position_lock:
                current_position[real_camid] = {
                    'pan': pan_val,
                    'tilt': tilt_val,
                    'roll': roll_val,
                    'zoom': zoom_val
                }
                global last_fdb_timestamp
                last_fdb_timestamp[real_camid] = time.time()
           
            # Update presets.py pan tracking
            try:
                # Bereken pan in graden
                pan_degrees = pan_val / 10.0
                # Update alleen de pan positie in de POC tracking
                presets.handle_feedback_pan(pan_degrees)
                if is_debug_mode():
                    print(f"[DEBUG] POC pan tracking bijgewerkt: Pan={pan_degrees:.1f}°")
            except Exception as e:
                print(f"[ERROR] Fout bij bijwerken POC pan tracking: {e}")
                
            if is_debug_mode():
                print(f"[DEBUG] Positiedata opgeslagen in current_position voor CamID {real_camid}: Pan: {pan_val}°, Tilt: {tilt_val}°, Roll: {roll_val}°, Zoom: {zoom_val}")
                
            # Retourneer een nieuwe FDB string met het juiste camID
            return f"FDB;{real_camid};{pan_val};{tilt_val};{roll_val};{zoom_val};"
        except ValueError as e:
            print(f"[ERROR] Failed to parse FDB message: {e}")
            return None
    return None


def stop_movement(as_name, send_apcr_command, apcr):
    st = movement_state[as_name]
    if st['active']:
        # Stuur idle-pakket voor deze as
        idle_packet_hex = get_idle_packet(as_name, apcr['camid'])
        if idle_packet_hex:
            data = bytes.fromhex(idle_packet_hex)
            logging.debug(f"[DEBUG] Sending idle packet for {as_name}: {data.hex()}")
            send_apcr_command(apcr, data)
        else:
            logging.debug(f"[ERROR] No idle packet found for {as_name}")

        st['active']    = False
        st['direction'] = None
        st['percentage'] = 0
        st.pop('control_type', None)  # Verwijder control_type indien aanwezig

        if st['timer']:
            st['timer'].cancel()
            st['timer'] = None

        # Na het stoppen, start een idle-timer, zodat we nogmaals een idle-pakket kunnen sturen
        # als de as lang genoeg inactief blijft.
        if st['idle_timer']:
            st['idle_timer'].cancel()
        st['idle_timer'] = threading.Timer(
            IDLE_DELAY,
            send_idle_if_still_inactive,
            args=[as_name, send_apcr_command, apcr]
        )
        st['idle_timer'].start()


def start_or_update_movement(as_name, direction, percentage, send_apcr_command, apcr, control_type='axis', axis_idx=None, settings=None):
    st = movement_state[as_name]
    changed = (
        st['direction'] != direction or
        st['percentage'] != percentage or
        st.get('control_type') != control_type
    )
    
    # Als we de beweging starten of updaten, pas dan adaptive speed toe voor pan/tilt als dat nodig is
    applying_adaptive_speed = False
    
    if control_type == 'axis' and axis_idx is not None and settings:
        device_id = apcr.get("device_id") or "default"
        dev_map   = settings.get("devices", {}).get(device_id, {})
        for m in dev_map.values():
            if isinstance(m, dict):
                if m.get("type") == "absolute_axis" and m.get("index") == axis_idx:
                    return          # deze axis is slider → geen beweging
                if m.get("type") == "virtual_axis" and axis_idx in (m.get("axis_0"), m.get("axis_1")):
                    return
            elif isinstance(m, list):
                if any(
                    (sub.get("type") == "absolute_axis" and sub.get("index") == axis_idx) or
                    (sub.get("type") == "virtual_axis"  and axis_idx in (sub.get("axis_0"), sub.get("axis_1")))
                    for sub in m
                ):
                    return
    
    original_percentage = percentage
    
    if as_name in ['pan', 'tilt', 'roll'] and settings and settings.get("global_settings", {}).get("adaptive_speed", False):
        # Haal het huidige zoom percentage op
        zoom_percentage = get_current_zoom_percentage(apcr)
        
        if zoom_percentage is not None:
            # Bereken de ABSOLUTE PTR-speed op basis van het zoom percentage
            # De originele percentage waarde wordt volledig genegeerd bij adaptive speed
            percentage = calculate_adaptive_speed(zoom_percentage, percentage, settings, apcr)  # Pass apcr parameter
            applying_adaptive_speed = True
            
            # Log dit als debug mode aan staat
            if is_debug_mode():
                print(f"[DEBUG] Adaptive Speed: {as_name} using speed {percentage}% " +
                      f"(determined by zoom level: {zoom_percentage:.1f}%, ignoring original speed)")
                
            # Forceer 'changed' op true als de snelheid is aangepast
            if st['percentage'] != percentage:
                changed = True

    if changed or not st['active']:
        st['active'] = True
        st['direction'] = direction
        st['percentage'] = percentage  # Dit is nu mogelijk de adaptive speed waarde
        st['control_type'] = control_type
        
        # Bewaar originele snelheid bij adaptive speed voor debugdoeleinden
        if applying_adaptive_speed:
            st['original_percentage'] = original_percentage

        # Verstuur het initiële commando met settings
        send_movement_packet(as_name, direction, percentage, send_apcr_command, apcr, control_type, settings)

        if st['timer']:
            st['timer'].cancel()
        # Voeg settings toe aan de args, zodat repeat_command(as_name, send_apcr_command, apcr, settings) wordt aangeroepen
        st['timer'] = threading.Timer(REPEAT_INTERVAL, repeat_command, args=[as_name, send_apcr_command, apcr, settings])
        st['timer'].start()

        if st['idle_timer']:
            st['idle_timer'].cancel()
            st['idle_timer'] = None
            
        # Notify interpreter about zoom activity if this is a zoom operation
        if as_name == 'zoom':
            try:
                # Import the interpreter module dynamically to avoid circular imports
                import importlib
                interpreter = importlib.import_module('interpreter')
                if hasattr(interpreter, 'notify_zoom_activity'):
                    interpreter.notify_zoom_activity(True)
            except Exception as e:
                logging.debug(f"Could not notify interpreter about zoom activity: {e}")

cumulative_delta_lock = threading.Lock()
cumulative_delta = 0  # Houdt de cumulatieve delta in graden bij
predicted_block = False  # Globale flag die aangeeft dat de voorspelde positie in de muur komt

def virtual_wall_predictor(settings, apcr, as_name, update_interval=0.05):
    """
    Een thread die continu de voorspelde positie berekent (op basis van de huidige positie en de cumulatieve delta)
    en de globale flag 'predicted_block' instelt als de voorspelde positie binnen de virtual wall komt.
    De marge wordt dynamisch bepaald op basis van het 'ptr_speed'-percentage uit de instellingen.
    """
    global predicted_block
    while True:
        pos_str = get_current_position(apcr)
        if pos_str:
            try:
                parts = pos_str.strip().split(';')
                if len(parts) >= 6:
                    # Gebruik de pan-waarde voor pan, anders tilt-waarde
                    current_pos = float(parts[2]) if as_name == "pan" else float(parts[3])
                else:
                    time.sleep(update_interval)
                    continue
            except Exception as e:
                print(f"[ERROR] Parsing current position in predictor: {e}")
                time.sleep(update_interval)
                continue

        # Haal het snelheidpercentage op uit de settings (voor pan/tilt)
        speed_percentage = settings["global_settings"].get("ptr_speed", 100)
        # Definieer een basis marge (bijv. 10°) en pas deze aan op basis van het percentage:
        base_margin = 20.0
        predictor_margin = base_margin * (speed_percentage / 100.0)
        
        # Bereken de cumulatieve delta en de voorspelde positie
        delta = get_cumulative_delta()
        predicted_pos = current_pos + delta

        # Haal de muurgrenzen op:
        if as_name == "pan":
            wall_start = settings["global_settings"].get("virtualwallstart_pan")
            wall_end = settings["global_settings"].get("virtualwallend_pan")
        else:
            wall_start = settings["global_settings"].get("virtualwallstart_tilt")
            wall_end = settings["global_settings"].get("virtualwallend_tilt")
        
        # Controleer met de update-functie of de voorspelde positie binnen de muur (met de extra marge) komt.
        # Hiervoor voegen we een extra margeparameter toe aan update_virtual_wall_state.
        block, _ = update_virtual_wall_state(
            as_name, predicted_pos, 0, wall_start, wall_end, apcr,
            virtualwall_active=settings["global_settings"].get("virtualwall", True),
            margin=predictor_margin
        )
        
        if block:
            if not predicted_block:
                print(f"[Predictor] Predicted position {predicted_pos:.2f}° is inside the wall (with margin {predictor_margin:.2f}°). Setting predicted_block = True")
            predicted_block = True
        else:
            if predicted_block:
                print(f"[Predictor] Predicted position {predicted_pos:.2f}° is outside the wall (with margin {predictor_margin:.2f}°). Clearing predicted_block.")
            predicted_block = False

        time.sleep(update_interval)

def update_virtual_wall_state(axis, current_pos, command_delta, apcr, virtualwall_active=True, margin=0):
    """
    Bepaalt of een nieuw stuurcommando (voor de opgegeven as) in het virtual wall‑gebied gaat.
    
    Args:
        axis: De as ('pan' of 'tilt')
        current_pos: Huidige positie in graden
        command_delta: Beweging in graden
        apcr: APC-R configuratie met virtual wall instellingen
        virtualwall_active: Of de virtual wall feature actief is
        margin: Extra marge in graden
    
    Returns:
        tuple: (block, trigger_correction, correction_delta)
    """
    # Als virtual wall uit staat, doe niets.
    if not virtualwall_active:
        # Reset de tracker als we buiten het gebied gaan
        virtual_wall_tracker[axis] = {}
        return False, False, 0
    
    # Haal de wall instellingen van de specifieke camera op
    if axis == "pan":
        wall_start = apcr.get("virtualwallstart_pan")
        wall_end = apcr.get("virtualwallend_pan")
    else:  # tilt
        wall_start = apcr.get("virtualwallstart_tilt")
        wall_end = apcr.get("virtualwallend_tilt")
    
    # Als deze camera geen virtual wall instellingen heeft, doe niets
    if wall_start is None or wall_end is None:
        virtual_wall_tracker[axis] = {}
        return False, False, 0

    expected_pos = current_pos + command_delta
    
    # Bepaal of we in absolute modus werken (als een grens buiten [0,360) valt)
    use_absolute = (wall_start < 0 or wall_start >= 360 or wall_end < 0 or wall_end >= 360)
    
    # Als we absolute waarden gebruiken, dan is de correcte vergelijking anders
    # afhankelijk van of wall_start > wall_end of wall_start < wall_end
    wall_boundaries_flipped = wall_start > wall_end
    
    # Haal de huidige tracker op voor deze as (of initialiseer deze)
    tracker = virtual_wall_tracker.get(axis, {})
    
    # Debug output
    print(f"[DEBUG-WALL-DETAIL] current_pos={current_pos}°, expected_pos={expected_pos}°")
    print(f"[DEBUG-WALL-DETAIL] wall_start={wall_start}°, wall_end={wall_end}°, flipped={wall_boundaries_flipped}")
    print(f"[DEBUG-WALL-DETAIL] use_absolute={use_absolute}, margin={margin}°")

    # Functie om te bepalen of een positie binnen de muur ligt:
    def pos_in_wall(pos):
        if use_absolute:
            if wall_boundaries_flipped:
                # Als wall_start > wall_end, dan moet de positie OFWEL >= wall_start OFWEL <= wall_end zijn
                return (pos >= (wall_start - margin)) or (pos <= (wall_end + margin))
            else:
                # Anders gebruiken we de normale inclusieve vergelijking
                return (wall_start - margin) <= pos <= (wall_end + margin)
        else:
            # Voor genormaliseerde hoeken, gebruik inWall helper functie
            return inWall(pos, wall_start, wall_end)
    
    # Check of huidige positie en verwachte positie in de muur zijn
    current_in_wall = pos_in_wall(current_pos)
    expected_in_wall = pos_in_wall(expected_pos)
    
    print(f"[DEBUG-WALL-DETAIL] current_in_wall={current_in_wall}, expected_in_wall={expected_in_wall}")
    
    # Reset de tracker als we buiten het muurgebied zitten
    if not current_in_wall and not expected_in_wall:
        virtual_wall_tracker[axis] = {}
        return False, False, 0
    
    # Als we nog buiten waren en nu met dit commando de muur in gaan:
    if not current_in_wall and expected_in_wall:
        # Registreer entry direction
        entry_direction = 1 if command_delta > 0 else -1
        tracker["entry_direction"] = entry_direction
        tracker["entry_position"] = current_pos
        virtual_wall_tracker[axis] = tracker
        print(f"[DEBUG-WALL-DETAIL] Beweging geblokkeerd: poging om muur binnen te gaan")
        return True, True, 0
        
    # Als er nog geen entry is geregistreerd, registreer die nu
    if current_in_wall and "entry_direction" not in tracker:
        # Als we al in de muur zijn, moeten we bepalen aan welke kant we zijn
        # Gebruiken we command_delta als indicatie
        entry_direction = 1 if command_delta > 0 else -1
        tracker["entry_direction"] = entry_direction
        tracker["entry_position"] = current_pos
        virtual_wall_tracker[axis] = tracker
        print(f"[DEBUG-WALL-DETAIL] Registreer entry_direction={entry_direction} voor as in muur")

    # Bepaal welke richting toegestaan is (opposite van entry direction)
    entry_direction = tracker.get("entry_direction", 0)
    allowed_exit_direction = -entry_direction  # de enige toegestane beweging als we in de muur zitten
    
    # Bepaal de richting van het huidige commando
    new_cmd_direction = 1 if command_delta > 0 else -1 if command_delta < 0 else 0
    
    # Als het commando in de exit-richting ligt, dan mag dat zonder correctie:
    if new_cmd_direction == allowed_exit_direction:
        print(f"[DEBUG-WALL-DETAIL] Beweging toegestaan: in exit richting ({new_cmd_direction})")
        return False, False, 0
    
    # Anders, bepaal de gewenste exit target
    # In absolute modus:
    if use_absolute:
        if wall_boundaries_flipped:
            # Als wall_start > wall_end,
            # Dan verlaten we via wall_start als entry_direction < 0,
            # en via wall_end als entry_direction > 0
            if entry_direction > 0:
                allowed_exit_target = wall_end + 1  # 1° voorbij wall_end
            else:
                allowed_exit_target = wall_start - 1  # 1° voor wall_start
        else:
            # Als wall_start < wall_end (normaal geval)
            if entry_direction > 0:
                allowed_exit_target = wall_start - 1
            else:
                allowed_exit_target = wall_end + 1
    else:
        # In genormaliseerde modus
        norm_current = norm360(current_pos)
        norm_wall_start = norm360(wall_start)
        norm_wall_end = norm360(wall_end)
        
        if entry_direction > 0:
            allowed_exit_target = (norm_wall_start - 1) % 360
        else:
            allowed_exit_target = (norm_wall_end + 1) % 360
    
    # Bereken correctie delta
    correction_delta = (allowed_exit_target - current_pos + 540) % 360 - 180
    
    print(f"[DEBUG-WALL-DETAIL] Beweging geblokkeerd: niet in exit richting. correction_delta={correction_delta}")
    
    # Geef nu aan dat het commando geblokkeerd moet worden en de correctie moet plaatsvinden.
    return True, True, correction_delta

# Globale variabele om de active track monitor thread bij te houden
_active_track_monitor_thread = None
_active_track_stop_event = None

def start_active_track_monitor(settings, send_apcr_command_func, save_settings_func):
    """
    Start een thread die active track posities monitort voor alle camera's.
    """
    global _active_track_monitor_thread, _active_track_stop_event
    
    # Stop een eventuele bestaande monitor thread
    stop_active_track_monitor()
    
    # Start een nieuwe monitor thread
    _active_track_stop_event = threading.Event()
    _active_track_monitor_thread = threading.Thread(
        target=active_track_position_monitor,
        args=(settings, send_apcr_command_func, save_settings_func, _active_track_stop_event),
        daemon=True
    )
    _active_track_monitor_thread.start()
    logging.debug("Active track position monitor gestart")

def stop_active_track_monitor():
    """
    Stop de active track monitor thread.
    """
    global _active_track_monitor_thread, _active_track_stop_event
    
    if _active_track_stop_event is not None:
        _active_track_stop_event.set()
    
    if _active_track_monitor_thread is not None and _active_track_monitor_thread.is_alive():
        _active_track_monitor_thread.join(timeout=1.0)
    
    _active_track_monitor_thread = None
    _active_track_stop_event = None
    logging.debug("Active track position monitor gestopt")

def active_track_position_monitor(settings, send_apcr_command_func, save_settings_func, stop_event):
    """
    Monitort alle camera's met active track ingeschakeld en controleert of ze niet
    in de virtual wall terechtkomen.
    """
    # Check frequentie - niet te vaak om CPU en netwerk belasting te beperken
    check_interval = 0.5  # seconden
    
    while not stop_event.is_set():
        try:
            # Controleer of virtual wall is ingeschakeld
            gs = settings.get("global_settings", {})
            virtual_wall_enabled = gs.get("virtualwall", False) or gs.get("virtualwallpreset", False)
            
            if not virtual_wall_enabled:
                time.sleep(check_interval)
                continue
            
            # Controleer alle APC-Rs met active track ingeschakeld
            for apcr in settings.get("apcrs", []):
                camid = apcr.get('camid')
                
                # Skip als active track niet ingeschakeld is
                if not apcr.get('active_track', False):
                    continue
                
                # Haal de positie op - eerst uit current_position
                position_data = None
                with position_lock:
                    if camid in current_position:
                        position_data = current_position[camid].copy()
                
                # Als we geen data hebben in current_position, probeer dan een opvragen
                if position_data is None:
                    pos_str = get_current_position(apcr)
                    if not pos_str:
                        continue
                    
                    # Parse de positie
                    parts = pos_str.strip().split(';')
                    if len(parts) < 6:
                        continue
                    
                    position_data = {
                        'pan': float(parts[2]),
                        'tilt': float(parts[3]),
                        'roll': float(parts[4]),
                        'zoom': float(parts[5])
                    }
                
                # Haal de virtual wall instellingen op voor deze specifieke camera
                wstart_pan = apcr.get("virtualwallstart_pan")
                wend_pan = apcr.get("virtualwallend_pan")
                
                # Skip als geen virtual wall instellingen aanwezig zijn voor deze camera
                if wstart_pan is None or wend_pan is None:
                    if is_debug_mode():
                        logging.debug(f"Camera {apcr['name']} heeft geen virtual wall pan instellingen, skipping check")
                    continue
                
                # Haal de pan positie op
                pan_raw = position_data.get('pan', 0)
                
                # Bereid de waarden voor op een consistente manier
                # Het is belangrijk te weten dat FDB waarden ALTIJD in tienden van graden zijn
                # en de virtual wall grenzen in settings.json ook
                pan_deg = pan_raw / 10.0  # Altijd delen door 10 omdat FDB waarden in tienden zijn
                wstart_deg = wstart_pan / 10.0  # Altijd delen door 10
                wend_deg = wend_pan / 10.0  # Altijd delen door 10
                
                # Normaliseer naar 0-360 bereik
                pan_norm = (pan_deg + 360) % 360
                wstart_norm = (wstart_deg + 360) % 360
                wend_norm = (wend_deg + 360) % 360
                
                if is_debug_mode():
                    logging.debug(f"Active track check voor {apcr['name']}:")
                    logging.debug(f"  Raw waarden: pos={pan_raw}, wstart={wstart_pan}, wend={wend_pan}")
                    logging.debug(f"  In graden: pos={pan_deg:.1f}°, wstart={wstart_deg:.1f}°, wend={wend_deg:.1f}°")
                    logging.debug(f"  Genormaliseerd: pos={pan_norm:.1f}°, wstart={wstart_norm:.1f}°, wend={wend_norm:.1f}°")
                
                # Bepaal of de positie binnen de muur valt
                in_wall = False
                
                # Scenario 1: De muur kruist niet de 0/360 grens
                if wstart_norm <= wend_norm:
                    in_wall = wstart_norm <= pan_norm <= wend_norm
                    if is_debug_mode():
                        logging.debug(f"Virtual wall kruist NIET de 0/360 grens. In wall? {in_wall}")
                # Scenario 2: De muur kruist de 0/360 grens
                else:
                    in_wall = pan_norm >= wstart_norm or pan_norm <= wend_norm
                    if is_debug_mode():
                        logging.debug(f"Virtual wall kruist de 0/360 grens. In wall? {in_wall}")
                
                if in_wall:
                    # Camera is in de muur - schakel active track uit
                    logging.warning(f"Camera {apcr['name']} is in virtual wall, positie: {pan_deg:.1f}° (genormaliseerd: {pan_norm:.1f}°)")
                    
                    # Schakel active track uit
                    apcr['active_track'] = False
                    
                    # Stuur active track commando
                    data = bytes.fromhex("08" + f"{camid:02x}" + "0400000e0b0000")
                    send_apcr_command_func(apcr, data)
                    
                    # Sla instellingen op
                    save_settings_func(settings)
                    
                    # Stuur notificatie naar de gebruiker
                    print(f"Active track automatisch uitgeschakeld voor {apcr['name']}: virtual wall overschreden ({pan_deg:.1f}°)")
        
        except Exception as e:
            logging.error(f"Fout in active track monitor: {e}")
            
        time.sleep(check_interval)


def repeat_command(as_name, send_apcr_command, apcr, settings, cumulative_delta=0):
    # Zorg dat we de last_input_time updaten:
    user_input_received()
    
    st = movement_state[as_name]
    if st['active']:
        # Voor pan/tilt/roll, pas adaptive speed toe als dat is ingeschakeld
        applying_adaptive_speed = False
        current_percentage = st['percentage']
        
        if as_name in ['pan', 'tilt', 'roll'] and settings and settings.get("global_settings", {}).get("adaptive_speed", False):
            # Haal het huidige zoom percentage op
            zoom_percentage = get_current_zoom_percentage(apcr)
            
            if zoom_percentage is not None:
                # Bereken de ABSOLUTE PTR-speed op basis van het zoom percentage
                # Het kan zijn dat de huidige percentage al een adaptive waarde is,
                # maar we berekenen het opnieuw om up-to-date te blijven met de zoom
                original_percentage = st.get('original_percentage', current_percentage)
                current_percentage = calculate_adaptive_speed(zoom_percentage, original_percentage, settings, apcr)  # Pass apcr parameter
                applying_adaptive_speed = True
                
                # Log dit als debug mode aan staat
                if is_debug_mode() and current_percentage != st['percentage']:
                    print(f"[DEBUG] Adaptive Speed (repeat): {as_name} using new speed: {current_percentage}% " +
                          f"(zoom level: {zoom_percentage:.1f}%)")
        
        # Bereken de delta voor dit herhalingscommando
        if as_name in ['pan', 'tilt']:
            if as_name == 'pan':
                base_min, base_max = (20, 2024) if st['direction'] == 'positive' else (-20, -2024)
            else:
                base_min, base_max = (-20, -2024) if st['direction'] == 'positive' else (20, 2024)
            # Gebruik current_percentage (mogelijk aangepast door adaptive speed)
            speed_val = int(base_min + (current_percentage - 1) * (base_max - base_min) / 99)
            command_delta = (speed_val / 2024.0) * 90.0
        else:
            command_delta = 0

        # Update cumulatieve delta
        update_cumulative_delta(command_delta)
        # Check de globale voorspelling:
        if predicted_block:
            print(f"[repeat_command] Predicted block actief: verdere herhalingen gestopt.")
            return  # Stop de herhaling

        # Anders, stuur het commando door met mogelijk aangepaste snelheid
        if current_percentage != st['percentage']:
            st['percentage'] = current_percentage  # Update de opgeslagen percentage
            
        send_movement_packet(as_name, st['direction'], current_percentage, send_apcr_command, apcr, st.get('control_type', 'axis'), settings)
        st['timer'] = threading.Timer(REPEAT_INTERVAL, repeat_command, args=[as_name, send_apcr_command, apcr, settings, get_cumulative_delta()])
        st['timer'].start()


def check_virtual_wall(axis, current, delta, wall_start, wall_end):
    """
    Controleer of een beoogde beweging (delta) op as 'axis' (pan of tilt)
    de virtuele muur (tussen wall_start en wall_end) inrijdt.
    
    Als de beweging de muur binnengaat:
      - Als de as nog buiten de muur is: retourneer (0, False) om de beweging te annuleren.
      - Als de as al in de muur zit: bereken dan de delta die de as 1° buiten de muur brengt
        (in de richting die de muur verlaat) en retourneer (corrected_delta, True).
    
    Als de beweging veilig is, retourneer (delta, False).
    """
    new_position = (current + delta) % 360.0
    # Als de beoogde nieuwe positie binnen de muur ligt, is de beweging niet veilig.
    if inWall(new_position, wall_start, wall_end):
        # Als we al binnen de muur zitten:
        if inWall(current, wall_start, wall_end):
            # Bereken afstand tot beide grenzen en kies de dichtstbijzijnde.
            lower = min(norm360(wall_start), norm360(wall_end))
            upper = max(norm360(wall_start), norm360(wall_end))
            dist_to_lower = (current - lower) if current >= lower else (current + 360 - lower)
            dist_to_upper = (upper - current) if current <= upper else (upper + 360 - current)
            if dist_to_lower < dist_to_upper:
                # Correctie: beweeg naar 1° buiten de lagere grens
                target = (lower - 1) % 360.0
            else:
                # Correctie: beweeg naar 1° buiten de hogere grens
                target = (upper + 1) % 360.0
            # Bepaal de benodigde delta (rekening houdend met wrapping)
            corrected_delta = (target - current + 540) % 360 - 180
            return corrected_delta, True
        else:
            # Als we nog buiten de muur zitten: negeer de beweging (return 0)
            return 0, False
    else:
        return delta, False


def get_base_packet(as_name, direction, camid):
    """
    Geeft het basis '1%' movement-pakket terug als bytes, met dynamische camid in de 2e byte.

    Let op: de laatste 2 speed-bytes worden later vervangen door de actuele speed-waarde.
    """
    # Template:
    #   0A <camid> 06 00000E06 80 00 14 00  (pan right 1%)
    #   0A <camid> 06 00000E06 80 00 EC FF  (pan left 1%)
    #   0A <camid> 06 00000E07 80 00 14 00  (tilt down 1%)
    #   0A <camid> 06 00000E07 80 00 EC FF  (tilt up 1%)
    #   0A <camid> 06 00000E08 80 00 14 00  (roll right 1%)
    #   0A <camid> 06 00000E08 80 00 EC FF  (roll left 1%)
    cam_hex = f"{camid:02x}"

    if as_name == 'pan':
        if direction == 'positive':  # right
            return bytes.fromhex(f"0A{cam_hex}0600000E0680001400")
        else:  # left
            return bytes.fromhex(f"0A{cam_hex}0600000E068000ECFF")

    elif as_name == 'tilt':
        if direction == 'positive':  # tilt down
            return bytes.fromhex(f"0A{cam_hex}0600000E0780001400")
        else:  # tilt up
            return bytes.fromhex(f"0A{cam_hex}0600000E078000ECFF")

    elif as_name == 'roll':
        if direction == 'positive':  # roll right
            return bytes.fromhex(f"0A{cam_hex}0600000E0880001400")
        else:  # roll left
            return bytes.fromhex(f"0A{cam_hex}0600000E088000ECFF")

    return None

def get_idle_packet(as_name, camid):
    """
    Geef het idle-pakket voor de opgegeven as, met dynamische camid in de 2e byte.
    """
    cam_hex = f"{camid:02x}"

    if as_name == 'pan':
        # 0A <camid> 06 00000E06 81 80 000000
        return f"0A{cam_hex}0600000E068180000000"
    elif as_name == 'tilt':
        # 0A <camid> 06 00000E07 81 80 000000
        return f"0A{cam_hex}0600000E078180000000"
    elif as_name == 'roll':
        # 0A <camid> 06 00000E08 80 000000
        return f"0A{cam_hex}0600000E0880000000"
    elif as_name == 'zoom':
        # 0A <camid> 06 00000009 80 000000
        return f"0A{cam_hex}060000000980000000"
    return None



def zoom_speed_increase(apcr, settings, save_settings_func):
    # Disable adaptive speed if enabled
    if settings["global_settings"].get("adaptive_speed", False):
        settings["global_settings"]["adaptive_speed"] = False
        print("Adaptive speed disabled by zoom speed increase")
    
    # Increase zoom speed
    current = settings["global_settings"].get('zoom_speed', 100)
    if current < 100:
        settings["global_settings"]['zoom_speed'] = current + 1
    save_settings_func(settings)
    print(f"Zoom speed is now {settings['global_settings']['zoom_speed']}")

def zoom_speed_decrease(apcr, settings, save_settings_func):
    # Disable adaptive speed if enabled
    if settings["global_settings"].get("adaptive_speed", False):
        settings["global_settings"]["adaptive_speed"] = False
        print("Adaptive speed disabled by zoom speed decrease")
    
    # Decrease zoom speed
    current = settings["global_settings"].get('zoom_speed', 100)
    if current > 1:
        settings["global_settings"]['zoom_speed'] = current - 1
    save_settings_func(settings)
    print(f"Zoom speed is now {settings['global_settings']['zoom_speed']}")

def ptr_speed_increase(apcr, settings, save_settings_func):
    # Get the effective PTR speed if adaptive speed is enabled
    effective_speed = settings["global_settings"].get('ptr_speed', 100)
    if settings["global_settings"].get("adaptive_speed", False):
        # Try to calculate the effective speed
        try:
            zoom_percentage = get_current_zoom_percentage(apcr)
            if zoom_percentage is not None:
                effective_speed = calculate_adaptive_speed(zoom_percentage, effective_speed, settings)
                # Round to nearest integer to avoid small decimals
                effective_speed = round(effective_speed)
        except Exception as e:
            logging.error(f"Error calculating effective speed: {e}")
        
        # Disable adaptive speed
        settings["global_settings"]["adaptive_speed"] = False
        print("Adaptive speed disabled by PTR speed increase")
        
        # Set PTR speed to the effective speed
        settings["global_settings"]['ptr_speed'] = effective_speed
    
    # Increase PTR speed
    current = settings["global_settings"].get('ptr_speed', 100)
    if current < 100:
        settings["global_settings"]['ptr_speed'] = current + 1
    save_settings_func(settings)
    print(f"PTR speed is now {settings['global_settings']['ptr_speed']}")

def ptr_speed_decrease(apcr, settings, save_settings_func):
    # Get the effective PTR speed if adaptive speed is enabled
    effective_speed = settings["global_settings"].get('ptr_speed', 100)
    if settings["global_settings"].get("adaptive_speed", False):
        # Try to calculate the effective speed
        try:
            zoom_percentage = get_current_zoom_percentage(apcr)
            if zoom_percentage is not None:
                effective_speed = calculate_adaptive_speed(zoom_percentage, effective_speed, settings)
                # Round to nearest integer to avoid small decimals
                effective_speed = round(effective_speed)
        except Exception as e:
            logging.error(f"Error calculating effective speed: {e}")
        
        # Disable adaptive speed
        settings["global_settings"]["adaptive_speed"] = False
        print("Adaptive speed disabled by PTR speed decrease")
        
        # Set PTR speed to the effective speed
        settings["global_settings"]['ptr_speed'] = effective_speed
    
    # Decrease PTR speed
    current = settings["global_settings"].get('ptr_speed', 100)
    if current > 1:
        settings["global_settings"]['ptr_speed'] = current - 1
    save_settings_func(settings)
    print(f"PTR speed is now {settings['global_settings']['ptr_speed']}")

def toggle_active_track(apcr, settings, save_settings_func, send_apcr_command_func):
    """
    Active Track commando (met dynamisch camid).
    Toggle de active_track status in de APC-R settings, stuur het overeenkomstige pakket,
    sla de gewijzigde settings op en print de status naar de console.
    """
    camid = apcr.get('camid', 1)
    data = bytes.fromhex("08" + f"{camid:02x}" + "0400000e0b0000")
    new_status = not apcr.get('active_track', False)
    apcr['active_track'] = new_status
    
    if new_status:
        print("Active track ON")
        # Zorg ervoor dat de monitor thread draait
        if (_active_track_monitor_thread is None or 
            not _active_track_monitor_thread.is_alive()):
            start_active_track_monitor(settings, send_apcr_command_func, save_settings_func)
    else:
        print("Active track OFF")
        # We stoppen de monitor alleen als ALLE camera's active track uit hebben
        any_active = False
        for ac in settings.get("apcrs", []):
            if ac.get('active_track', False):
                any_active = True
                break
        
        if not any_active:
            stop_active_track_monitor()
    
    send_apcr_command_func(apcr, data)
    save_settings_func(settings)  
    return f"ACTIVE_TRACK set to {new_status}"


def send_recenter(apcr, send_apcr_command_func):
    """
    Recenter command (with dynamic camid).
    """
    try:
        camid = int(apcr['camid']) if 'camid' in apcr else 1
        data = bytes.fromhex("08" + f"{camid:02x}" + "0400000e0c0000")
        print("[DEBUG] Recenter data to send:", data.hex())
        send_apcr_command_func(apcr, data)
        print("Recenter command sent.")
    except Exception as e:
        print(f"[ERROR] Failed to recenter: {e}")

def handle_pan_tilt_speed_shortcut(apcr, settings, save_settings_func):
    """
    Elastische shortcut voor ptr_speed: 100 -> 75 -> 50 -> 25 -> 5 -> 1 en weer omhoog.
    Als adaptive speed actief is, wordt direct de dichtstbijzijnde stap gekozen
    en wordt adaptive speed uitgeschakeld.
    """
    speed_steps = [100, 75, 50, 25, 5, 1]
    current_direction = settings["global_settings"].get("ptr_speed_direction", "down")
    
    # Get the current effective speed
    effective_speed = settings["global_settings"].get('ptr_speed', 100)
    
    if settings["global_settings"].get("adaptive_speed", False):
        # Try to calculate the effective speed
        try:
            zoom_percentage = get_current_zoom_percentage(apcr)
            if zoom_percentage is not None:
                effective_speed = calculate_adaptive_speed(zoom_percentage, effective_speed, settings)
                # Round to nearest integer to avoid small decimals
                effective_speed = round(effective_speed)
        except Exception as e:
            logging.error(f"Error calculating effective speed: {e}")
        
        # Disable adaptive speed
        settings["global_settings"]["adaptive_speed"] = False
        print("Adaptive speed disabled by PTR speed shortcut")
        
        # Find the closest step to the current effective speed
        closest_speed = min(speed_steps, key=lambda x: abs(x - effective_speed))
        
        # Set PTR speed directly to the closest step
        settings["global_settings"]['ptr_speed'] = closest_speed
        print(f"[DEBUG] PTR speed set directly to closest step: {closest_speed}% (from adaptive {effective_speed}%)")
        
        # Also update the direction for the next shortcut press
        current_idx = speed_steps.index(closest_speed)
        if current_idx == 0:  # At max speed, direction should be down
            settings["global_settings"]["ptr_speed_direction"] = "down"
        elif current_idx == len(speed_steps) - 1:  # At min speed, direction should be up
            settings["global_settings"]["ptr_speed_direction"] = "up"
        else:
            # Keep the current direction
            pass
            
    else:
        # If adaptive speed is not active, use the normal elastic shortcut behavior
        current_speed = settings["global_settings"].get("ptr_speed", 100)
        if current_speed not in speed_steps:
            current_speed = min(speed_steps, key=lambda x: abs(x - current_speed))
        
        idx = speed_steps.index(current_speed)
        
        if current_direction == "down":
            if idx < len(speed_steps) - 1:
                next_speed = speed_steps[idx + 1]
            else:
                next_speed = speed_steps[idx - 1]
                settings["global_settings"]["ptr_speed_direction"] = "up"
        else:
            if idx > 0:
                next_speed = speed_steps[idx - 1]
            else:
                next_speed = speed_steps[idx + 1]
                settings["global_settings"]["ptr_speed_direction"] = "down"
        
        settings["global_settings"]['ptr_speed'] = next_speed
        print(f"[DEBUG] PTR speed set to {next_speed} and direction to {settings['global_settings'].get('ptr_speed_direction')}")
    
    save_settings_func(settings)

# Gecorrigeerde functies in controls.py

def zoom_speed_increase(apcr, settings, save_settings_func):
    # Verwijderd: code om adaptive speed uit te schakelen
    
    # Increase zoom speed
    current = settings["global_settings"].get('zoom_speed', 100)
    if current < 100:
        settings["global_settings"]['zoom_speed'] = current + 1
    save_settings_func(settings)
    print(f"Zoom speed is now {settings['global_settings']['zoom_speed']}")

def zoom_speed_decrease(apcr, settings, save_settings_func):
    # Verwijderd: code om adaptive speed uit te schakelen
    
    # Decrease zoom speed
    current = settings["global_settings"].get('zoom_speed', 100)
    if current > 1:
        settings["global_settings"]['zoom_speed'] = current - 1
    save_settings_func(settings)
    print(f"Zoom speed is now {settings['global_settings']['zoom_speed']}")

def handle_zoom_speed_shortcut(apcr, settings, save_settings_func):
    """
    Elastische shortcut voor zoom_speed: 100 -> 75 -> 50 -> 25 -> 5 -> 1 en weer omhoog.
    """
    # Verwijderd: code om adaptive speed uit te schakelen
    
    # Zoom speed shortcut logic
    current_speed = settings["global_settings"].get("zoom_speed", 100)
    speed_steps = [100, 75, 50, 25, 5, 1]
    current_direction = settings["global_settings"].get("zoom_speed_direction", "down")

    if current_speed not in speed_steps:
        current_speed = min(speed_steps, key=lambda x: abs(x - current_speed))

    if current_direction == "down":
        idx = speed_steps.index(current_speed)
        if idx < len(speed_steps) - 1:
            next_speed = speed_steps[idx + 1]
        else:
            next_speed = speed_steps[idx - 1]
            settings["global_settings"]["zoom_speed_direction"] = "up"
    else:
        idx = speed_steps.index(current_speed)
        if idx > 0:
            next_speed = speed_steps[idx - 1]
        else:
            next_speed = speed_steps[idx + 1]
            settings["global_settings"]["zoom_speed_direction"] = "down"

    settings["global_settings"]['zoom_speed'] = next_speed
    save_settings_func(settings)
    print(f"[DEBUG] Zoom speed set to {next_speed} and direction to {settings['global_settings'].get('zoom_speed_direction')}")


class UDPListener(threading.Thread):
    """
    Central UDP listener class that receives FDB messages and places them in the position_queue.
    This class also handles automatic recovery after errors.
    """
    def __init__(self, ip="0.0.0.0", port=11582, timeout=1.0, settings=None):
        super().__init__(daemon=True)
        self.ip = ip
        self.port = port
        self.timeout = timeout
        self.socket = None
        self.running = False
        self.stop_event = threading.Event()
        self.error_delay = 1.0  # Seconds to wait after an error before trying again
        self.max_error_delay = 30.0  # Maximum wait time between recovery attempts
        self.current_error_delay = self.error_delay
        self.settings = settings  # Add settings for camID lookup
        self._initialize_socket()
        
        # Create a mapping from IP address to camid for quicker lookups
        self.ip_to_camid_map = {}
        self._update_ip_camid_map()

    def _update_ip_camid_map(self):
        """
        Update the mapping from IP addresses to camids based on current settings.
        This helps with quick lookups when receiving FDB messages.
        """
        self.ip_to_camid_map = {}
        if self.settings:
            for apcr in self.settings.get("apcrs", []):
                if "ip" in apcr and "camid" in apcr:
                    self.ip_to_camid_map[apcr["ip"]] = apcr["camid"]
            
            if is_debug_mode():
                print(f"[DEBUG] Updated IP-to-CamID map: {self.ip_to_camid_map}")

    def _initialize_socket(self):
        """
        Initialize the UDP socket for reception.
        """
        try:
            if self.socket:
                try:
                    self.socket.close()
                except:
                    pass
                
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self.socket.bind((self.ip, self.port))
            self.socket.settimeout(self.timeout)
            logging.info(f"UDP Listener initialized on {self.ip}:{self.port}")
            return True
        except Exception as e:
            logging.error(f"Error initializing UDP socket: {e}")
            return False


    def _process_device_status_response(self, data, sender_addr):
        """
        Process a status response from an APC-R device.
        If the device exists in settings but has a different IP or CamID,
        automatically update the settings.
        
        Args:
            data: The response data
            sender_addr: Tuple (ip, port) of the sending device
        
        Returns:
            bool: True if settings were updated
        """
        if not self.settings:
            return False
            
        try:
            # Decode the response
            resp_str = data.decode(errors='ignore')
            
            # Check if this is a status response (contains '|' separators)
            if '|' not in resp_str:
                return False
                
            parts = resp_str.split('|')
            if len(parts) < 8:
                return False
                
            # Extract device information
            device_name = parts[0].strip()
            try:
                device_camid = int(parts[-2])  # CamID is second-to-last field
            except (ValueError, IndexError):
                return False
                
            sender_ip = sender_addr[0]
            
            # Check if we have this device in our settings (by name)
            matching_apcr = None
            matching_index = None
            
            for i, apcr in enumerate(self.settings.get("apcrs", [])):
                if apcr.get("name") == device_name:
                    matching_apcr = apcr
                    matching_index = i
                    break
                    
            if matching_apcr:
                # Check if IP or CamID has changed
                old_ip = matching_apcr.get("ip")
                old_camid = matching_apcr.get("camid")
                
                changes_detected = False
                
                if old_ip != sender_ip:
                    changes_detected = True
                    self.settings["apcrs"][matching_index]["ip"] = sender_ip
                    logging.info(f"Auto-updated APC-R '{device_name}' IP: {old_ip} -> {sender_ip}")
                    
                if old_camid != device_camid:
                    changes_detected = True
                    self.settings["apcrs"][matching_index]["camid"] = device_camid
                    logging.info(f"Auto-updated APC-R '{device_name}' CamID: {old_camid} -> {device_camid}")
                    
                if changes_detected:
                    # Save the updated settings
                    try:
                        from main import save_settings
                        save_settings(self.settings)
                        logging.info(f"Successfully saved auto-updated settings for APC-R '{device_name}'")
                        print(f"[AUTO-UPDATE] APC-R '{device_name}' settings were automatically updated.")
                        return True
                    except Exception as e:
                        logging.error(f"Failed to save auto-updated settings: {e}")
            
            # Also update the ip_to_camid_map to ensure correct camID mapping
            if device_name and sender_ip and device_camid:
                self.ip_to_camid_map[sender_ip] = device_camid
                        
            return False
                
        except Exception as e:
            logging.error(f"Error processing device status response: {e}")
            return False

    def start(self):
        """
        Start the UDP listener in a separate thread.
        """
        if self.running:
            logging.warning("UDP Listener is already running, start() ignored")
            return False
        
        self.stop_event.clear()
        self.running = True
        self.thread = threading.Thread(target=self._listen_loop, daemon=True)
        self.thread.start()
        logging.info("UDP Listener started")
        return True

    def stop(self):
        """
        Stop the UDP listener.
        """
        self.running = False
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2.0)
        if self.socket:
            try:
                self.socket.close()
            except:
                pass
        logging.info("UDP Listener stopped")

    def _listen_loop(self):
        """
        Main loop function for receiving UDP messages.
        Now includes automatic settings update when device configuration changes.
        """
        consecutive_timeouts = 0
        max_timeouts_before_reset = 10
        
        while self.running and not self.stop_event.is_set():
            try:
                # Try to receive a message
                data, addr = self.socket.recvfrom(BUFFER_SIZE)
                consecutive_timeouts = 0
                
                # Debug output for received packets
                if is_debug_mode():
                    print(f"[DEBUG] Received packet from {addr}: {data.hex()}")
                
                # Check if this is potentially a status response (format: NAME|NAME|ETH|...)
                if b'|' in data and addr[1] == 2390:  # Status responses come from port 2390
                    # Try to process as device status response
                    self._process_device_status_response(data, addr)
                
                # Process the message if it's a valid FDB message
                message = data.decode(errors='ignore')
                if message.startswith("FDB;"):
                    # Make sure our IP-to-CamID map is up-to-date
                    if not self.ip_to_camid_map:
                        self._update_ip_camid_map()
                        
                    # Get the sender's IP address
                    sender_ip = addr[0]
                    
                    # Look up the camid based on IP address
                    real_camid = None
                    if sender_ip in self.ip_to_camid_map:
                        real_camid = self.ip_to_camid_map[sender_ip]
                    else:
                        # If we can't find the IP in our map, try to parse the camid from the message
                        try:
                            parts = message.strip().split(';')
                            if len(parts) >= 2:
                                parsed_camid = int(parts[1])
                                
                                # Check if this camid exists in our settings
                                camid_exists = False
                                for apcr in self.settings.get("apcrs", []):
                                    if apcr.get("camid") == parsed_camid:
                                        camid_exists = True
                                        # Update our map with this IP-to-camid mapping
                                        self.ip_to_camid_map[sender_ip] = parsed_camid
                                        break
                                
                                if camid_exists:
                                    real_camid = parsed_camid
                                    if is_debug_mode():
                                        print(f"[DEBUG] Learned new IP-to-CamID mapping: {sender_ip} -> {real_camid}")
                                else:
                                    logging.warning(f"Received FDB message with unknown CamID {parsed_camid} from {sender_ip}")
                        except Exception as e:
                            logging.error(f"Failed to parse camid from FDB message: {e}")
                    
                    if real_camid is not None:
                        # Parse the rest of the message
                        try:
                            parts = message.strip().split(';')
                            if len(parts) >= 6:
                                pos = {
                                    'camid': real_camid,
                                    'pan': float(parts[2]),
                                    'tilt': float(parts[3]),
                                    'roll': float(parts[4]),
                                    'zoom': float(parts[5])
                                }
                                
                                # Put in queue for processing
                                position_queue.put(pos)
                                
                                # Debug output for position data
                                if is_debug_mode():
                                    print(f"[DEBUG] CamID {pos['camid']} position from {sender_ip}: Pan: {pos['pan']:.2f}°, Tilt: {pos['tilt']:.2f}°, Roll: {pos['roll']:.2f}°, Zoom: {pos['zoom']}")
                                
                                # Update the current_position dictionary directly
                                with position_lock:
                                    current_position[real_camid] = {
                                        'pan': pos['pan'],
                                        'tilt': pos['tilt'],
                                        'roll': pos['roll'],
                                        'zoom': pos['zoom']
                                    }
                                    last_fdb_timestamp[real_camid] = time.time()
                                    
                                # Update presets.py pan tracking
                                try:
                                    # Calculate pan in degrees
                                    pan_degrees = pos['pan'] / 10.0
                                    # Update only the pan position in POC tracking
                                    presets.handle_feedback_pan(pan_degrees)
                                    if is_debug_mode():
                                        print(f"[DEBUG] UDPListener: POC pan tracking updated: Pan={pan_degrees:.1f}°")
                                except Exception as e:
                                    logging.error(f"Error updating POC pan tracking in UDPListener: {e}")
                            else:
                                logging.warning(f"Invalid FDB message format from {sender_ip}: {message}")
                        except Exception as e:
                            logging.error(f"Error processing FDB message: {e}")
                    else:
                        logging.warning(f"Received FDB message from unknown IP {sender_ip}, message: {message}")
                
                # Reset error delay if we successfully receive messages
                self.current_error_delay = self.error_delay
                
            except socket.timeout:
                consecutive_timeouts += 1
                if consecutive_timeouts >= max_timeouts_before_reset:
                    logging.warning(f"{consecutive_timeouts} consecutive timeouts. Resetting socket.")
                    self._initialize_socket()
                    consecutive_timeouts = 0
                continue
                
            except Exception as e:
                logging.error(f"Error in UDP listener: {e}")
                time.sleep(self.current_error_delay)
                self.current_error_delay = min(self.current_error_delay * 2, self.max_error_delay)
                
                if not self._initialize_socket():
                    logging.error(f"Could not reinitialize UDP socket, trying again in {self.current_error_delay} seconds")
                else:
                    self.current_error_delay = self.error_delay
                    consecutive_timeouts = 0

        logging.info("UDP listen_loop terminated")

def init_udp_listener(settings):
    """
    Initialize the global UDP listener instance.
    """
    global _udp_listener_instance
    
    if _udp_listener_instance is not None:
        _udp_listener_instance.stop()
    
    # Get IP and port from settings
    ip = settings.get("listener_ip", "0.0.0.0")
    port = settings.get("listener_port", 11582)
    
    # Create a new UDPListener and pass the settings for camID lookup
    _udp_listener_instance = UDPListener(ip, port, settings=settings)
    return _udp_listener_instance.start()

def get_udp_listener_instance():
    """
    Verkrijg de huidige UDP listener instance.
    """
    global _udp_listener_instance
    return _udp_listener_instance

def wait_for_current_position(apcr=None, timeout=5):
    """
    Wait until a position update is available within 'timeout' seconds.
    First, the queue is emptied so that old messages are not used.
    Returns a dictionary with the current position in degrees:
      {'camid': ..., 'pan': ..., 'tilt': ..., 'roll': ..., 'zoom': ...}
    If no new data arrives within the timeout, the most recent position
    from the global 'current_position' dictionary is used (if available).
    
    Args:
        apcr: Optional APC-R to specify which camera's position to wait for
        timeout: Maximum time to wait in seconds
        
    Returns:
        dict: Position data or None if no data is available
    """
    # First clear the queue
    while not position_queue.empty():
        try:
            position_queue.get_nowait()
        except queue.Empty:
            break

    target_camid = None
    if apcr and 'camid' in apcr:
        target_camid = apcr.get('camid')
        if is_debug_mode():
            print(f"[DEBUG] wait_for_current_position looking for CamID {target_camid}")

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            pos = position_queue.get(timeout=0.1)
            # If apcr is specified, check if the camid matches
            if target_camid is not None and pos['camid'] != target_camid:
                if is_debug_mode():
                    print(f"[DEBUG] Skipping position data for CamID {pos['camid']}, waiting for {target_camid}")
                continue
            return pos
        except queue.Empty:
            # If the queue is empty, try to get the most recent live position
            with position_lock:
                if target_camid is not None and target_camid in current_position:
                    pos_data = current_position[target_camid]
                    return {
                        'camid': target_camid,
                        'pan': pos_data['pan'],
                        'tilt': pos_data['tilt'],
                        'roll': pos_data['roll'],
                        'zoom': pos_data['zoom']
                    }
                elif target_camid is None and current_position:
                    # If no specific camid is requested, return the first available
                    first_camid = next(iter(current_position))
                    pos_data = current_position[first_camid]
                    return {
                        'camid': first_camid,
                        'pan': pos_data['pan'],
                        'tilt': pos_data['tilt'],
                        'roll': pos_data['roll'],
                        'zoom': pos_data['zoom']
                    }
   
    if is_debug_mode():
        if target_camid is not None:
            print(f"[DEBUG] Timeout ({timeout}s) expired without receiving position data for CamID {target_camid}")
        else:
            print(f"[DEBUG] Timeout ({timeout}s) expired without receiving any position data")
    
    logging.error(f"No current position available after waiting {timeout}s")
    return None

def get_current_position(apcr):
    """
    Get the current position for the given APC-R (using apcr['camid']) from the global current_position.
    This function returns an FDB string, similar to:
      "FDB;camid;pan;tilt;roll;zoom;"
      
    Args:
        apcr: Dictionary containing at least a 'camid' key
        
    Returns:
        str: FDB-formatted position string or None if position is not available
    """
    if not apcr:
        if is_debug_mode():
            print("[DEBUG] get_current_position called with None apcr")
        return None
    
    cam_id = apcr.get('camid')
    if cam_id is None:
        if is_debug_mode():
            print("[DEBUG] get_current_position called with apcr that has no camid")
        return None
    
    if is_debug_mode():
        print(f"[DEBUG] get_current_position called for CamID {cam_id}")
        # Show all available camids in current_position
        with position_lock:
            available_camids = list(current_position.keys())
        print(f"[DEBUG] Available CamIDs in current_position: {available_camids}")
    
    with position_lock:
        pos = current_position.get(cam_id)
    
    if pos:
        result = f"FDB;{cam_id};{pos['pan']};{pos['tilt']};{pos['roll']};{pos['zoom']};"
        
        if is_debug_mode():
            print(f"[DEBUG] getCurrentPosition found: CamID {cam_id}: Pan: {pos['pan']}°, Tilt: {pos['tilt']}°, Roll: {pos['roll']}°, Zoom: {pos['zoom']}")
        
        return result
    else:
        if is_debug_mode():
            print(f"[DEBUG] getCurrentPosition: No position data available for CamID {cam_id}!")
        
        return None

def norm360(x):
    """Normaliseer een hoek zodat deze tussen 0 en 360° ligt."""
    return x % 360.0

def inWall(x, w1, w2):
    """
    Bepaalt of x (in graden) binnen de virtual wall–zone ligt,
    gedefinieerd door w1 en w2 (onder- en bovengrens).
    """
    lower = min(norm360(w1), norm360(w2))
    upper = max(norm360(w1), norm360(w2))
    return lower <= x <= upper


def send_idle_if_still_inactive(as_name, send_apcr_command, apcr):
    """
    Verzendt het idle-pakket als de beweging nog steeds inactief is.
    """
    st = movement_state[as_name]
    if not st['active']:  # nog steeds inactief
        idle_packet_hex = get_idle_packet(as_name, apcr['camid'])
        if idle_packet_hex:
            data = bytes.fromhex(idle_packet_hex)
            logging.debug(f"[DEBUG] Sending idle packet after delay for {as_name}: {data.hex()}")
            send_apcr_command(apcr, data)
            
            # We don't notify about zoom here anymore
            # The notification already happened in stop_movement
            # This prevents multiple notifications that could cause issues
        else:
            logging.debug(f"[ERROR] No idle packet found for {as_name}")


def update_cumulative_delta(delta):
    global cumulative_delta
    with cumulative_delta_lock:
        cumulative_delta += delta

def reset_cumulative_delta():
    global cumulative_delta
    with cumulative_delta_lock:
        cumulative_delta = 0

def get_cumulative_delta():
    global cumulative_delta
    with cumulative_delta_lock:
        return cumulative_delta



# Complete send_movement_packet function with zoom notification

def send_movement_packet(as_name, direction, percentage, send_apcr_command, apcr, control_type='axis', settings=None):
    try:
        logging.debug(f"[DEBUG] send_movement_packet(as_name='{as_name}', direction='{direction}', percentage={percentage}, control_type='{control_type}')")
        camid = apcr.get('camid', 1)

        if as_name in ['pan', 'tilt']:
            # Bereken de snelheid (voorbeeldberekening)
            if as_name == 'pan':
                base_min, base_max = (20, 2024) if direction == 'positive' else (-20, -2024)
            else:  # tilt
                base_min, base_max = (-20, -2024) if direction == 'positive' else (20, 2024)
            speed_val = int(base_min + (percentage - 1) * (base_max - base_min) / 99)
            speed_bytes = struct.pack('<h', speed_val)
            base_packet = get_base_packet(as_name, direction, camid)
            if base_packet is None:
                logging.error(f"[ERROR] No base packet found for: {as_name}, {direction}")
                return
            packet_prefix = base_packet[:-2]
            new_packet = packet_prefix + speed_bytes
            data = new_packet

        # Voeg roll toe aan de ondersteunde assen
        if as_name in ['pan', 'tilt', 'roll']:
            # Bereken de snelheid (afhankelijk van as)
            if as_name == 'pan':
                base_min, base_max = (20, 2024) if direction == 'positive' else (-20, -2024)
            elif as_name == 'tilt':
                base_min, base_max = (-20, -2024) if direction == 'positive' else (20, 2024)
            elif as_name == 'roll':
                # Roll gebruikt dezelfde logica als pan
                base_min, base_max = (20, 2024) if direction == 'positive' else (-20, -2024)
                
            speed_val = int(base_min + (percentage - 1) * (base_max - base_min) / 99)
            speed_bytes = struct.pack('<h', speed_val)
            
            base_packet = get_base_packet(as_name, direction, camid)
            if base_packet is None:
                logging.error(f"[ERROR] No base packet found for: {as_name}, {direction}")
                return
                
            packet_prefix = base_packet[:-2]
            new_packet = packet_prefix + speed_bytes
            data = new_packet


            # --- Virtual Wall Integration ---
            if settings is not None:
                # Retrieve global_settings (ensure it's not None)
                global_settings = settings.get("global_settings", {})
                if global_settings is None:
                    logging.warning("settings['global_settings'] is None; skipping virtual wall check.")
                else:
                    # Explicitly check if virtual wall is enabled
                    virtualwall_enabled = global_settings.get("virtualwall", False)
                    
                    if virtualwall_enabled:
                        # Nu halen we de virtual wall grenzen op uit de camera-specifieke instellingen
                        if as_name == "pan":
                            wall_start = apcr.get("virtualwallstart_pan")
                            wall_end = apcr.get("virtualwallend_pan")
                        else:
                            wall_start = apcr.get("virtualwallstart_tilt")
                            wall_end = apcr.get("virtualwallend_tilt")
                            
                        # Alleen de virtual wall check uitvoeren als er instellingen beschikbaar zijn voor deze camera
                        if wall_start is not None and wall_end is not None:
                            logging.debug(f"[VIRTUAL WALL] Check active for {as_name}: {wall_start}° to {wall_end}°")
                            
                            # Obtain the current position of the axis
                            current_pos = None
                            pos_str = get_current_position(apcr)
                            if pos_str:
                                try:
                                    parts = pos_str.strip().split(';')
                                    if len(parts) >= 6:
                                        current_pos = float(parts[2]) if as_name=="pan" else float(parts[3])
                                except Exception as e:
                                    logging.error(f"[ERROR] Parsing current position: {e}")
                            
                            if current_pos is not None:
                                # Calculate the movement in degrees (approximation)
                                command_delta = (speed_val / 2024.0) * 90.0  
                                logging.debug(f"[VIRTUAL WALL] Current position: {current_pos}°, command_delta: {command_delta}°")
                                
                                # Determine if the position is within the wall (absolute mode)
                                wall_boundaries_flipped = wall_start > wall_end
                                
                                # Check if current position is within the wall
                                current_in_wall = False
                                if wall_boundaries_flipped:
                                    current_in_wall = (current_pos >= wall_start) or (current_pos <= wall_end)
                                else:
                                    current_in_wall = (wall_start <= current_pos <= wall_end)
                                
                                # Calculate the expected position
                                expected_pos = current_pos + command_delta
                                
                                # Check if the expected position is within the wall
                                expected_in_wall = False
                                if wall_boundaries_flipped:
                                    expected_in_wall = (expected_pos >= wall_start) or (expected_pos <= wall_end)
                                else:
                                    expected_in_wall = (wall_start <= expected_pos <= wall_end)
                                
                                # Movement direction (positive or negative)
                                moving_direction = 1 if command_delta > 0 else -1 if command_delta < 0 else 0
                                
                                # Debug output
                                logging.debug(f"[VIRTUAL WALL] current_in_wall={current_in_wall}, expected_in_wall={expected_in_wall}, moving_direction={moving_direction}")
                                
                                # LOGIC FOR ALLOWING MOVEMENT
                                if current_in_wall:
                                    # We are already inside the wall - determine the exit direction
                                    exit_direction = 0
                                    if wall_boundaries_flipped:
                                        # If wall_start > wall_end, then the exit direction depends on our current position
                                        if current_pos >= wall_start:
                                            # We are above the upper boundary, exit = downward
                                            exit_direction = -1
                                        else:
                                            # We are below the lower boundary, exit = upward
                                            exit_direction = 1
                                    else:
                                        # Normal case (wall_start < wall_end)
                                        exit_middle = (wall_start + wall_end) / 2
                                        if current_pos < exit_middle:
                                            # We are closer to the lower boundary, exit = downward
                                            exit_direction = -1
                                        else:
                                            # We are closer to the upper boundary, exit = upward
                                            exit_direction = 1
                                    
                                    logging.debug(f"[VIRTUAL WALL] exit_direction={exit_direction}")
                                    
                                    # If we are moving in the exit direction, it is allowed
                                    if moving_direction == exit_direction:
                                        logging.debug(f"[VIRTUAL WALL] Movement in exit direction allowed!")
                                        # Allow the movement to continue (no return)
                                    else:
                                        # Attempting to move further into the virtual wall - blocking!
                                        print(f"[VIRTUAL WALL] Movement of {as_name} further INTO virtual wall is blocked.")
                                        
                                        # Send an IDLE packet
                                        idle_hex = get_idle_packet(as_name, camid)
                                        if idle_hex:
                                            send_apcr_command(apcr, bytes.fromhex(idle_hex))
                                            logging.debug(f"[VIRTUAL WALL] IDLE packet sent for {as_name}")
                                        
                                        return  # Block the command
                                
                                elif not current_in_wall and expected_in_wall:
                                    # We are just about to enter the wall - blocking!
                                    print(f"[VIRTUAL WALL] Attempt to move {as_name} INTO virtual wall is blocked.")
                                    
                                    # Send an IDLE packet
                                    idle_hex = get_idle_packet(as_name, camid)
                                    if idle_hex:
                                        send_apcr_command(apcr, bytes.fromhex(idle_hex))
                                        logging.debug(f"[VIRTUAL WALL] IDLE packet sent for {as_name}")
                                    
                                    return  # Block the command
                                
                                else:
                                    # Normal movement outside the wall - no issues
                                    logging.debug(f"[VIRTUAL WALL] Normal movement outside the wall allowed.")
                                    # Allow the movement to continue (no return)
                                    # Allow the movement to continue (no return)

        elif as_name == 'zoom':
            packet = get_zoom_packet(percentage, direction, control_type, camid)
            if packet is None:
                logging.error("[ERROR] Failed to get zoom packet.")
                return
            data = packet
            logging.debug(f"[DEBUG] Sending zoom movement: direction={direction}, percentage={percentage}, control_type={control_type}")
            logging.debug(f"[DEBUG] Zoom Packet to send: {data.hex()}")
        else:
            logging.error(f"[ERROR] Unknown as_name '{as_name}'")
            return

        send_apcr_command(apcr, data)
        logging.debug(f"[DEBUG] Sent packet: {data.hex()}")
        
        # Notify about zoom activity if this is a zoom operation
        if as_name == 'zoom':
            try:
                # Import the interpreter module dynamically to avoid circular imports
                import importlib
                interpreter = importlib.import_module('interpreter')
                if hasattr(interpreter, 'notify_zoom_activity'):
                    interpreter.notify_zoom_activity(True)
                    logging.debug(f"[DEBUG] Notified interpreter that zoom is active")
            except Exception as e:
                logging.debug(f"Could not notify interpreter about zoom activity: {e}")

    except Exception as e:
        logging.error(f"[ERROR] Failed to send movement packet: {e}")

# Complete stop_movement function with zoom notification

def stop_movement(as_name, send_apcr_command, apcr):
    st = movement_state[as_name]
    if st['active']:
        # Stuur idle-pakket voor deze as
        idle_packet_hex = get_idle_packet(as_name, apcr['camid'])
        if idle_packet_hex:
            data = bytes.fromhex(idle_packet_hex)
            logging.debug(f"[DEBUG] Sending idle packet for {as_name}: {data.hex()}")
            send_apcr_command(apcr, data)
        else:
            logging.debug(f"[ERROR] No idle packet found for {as_name}")

        st['active']    = False
        st['direction'] = None
        st['percentage'] = 0
        st.pop('control_type', None)  # Verwijder control_type indien aanwezig

        if st['timer']:
            st['timer'].cancel()
            st['timer'] = None

        # Na het stoppen, start een idle-timer, zodat we nogmaals een idle-pakket kunnen sturen
        # als de as lang genoeg inactief blijft.
        if st['idle_timer']:
            st['idle_timer'].cancel()
        st['idle_timer'] = threading.Timer(
            IDLE_DELAY,
            send_idle_if_still_inactive,
            args=[as_name, send_apcr_command, apcr]
        )
        st['idle_timer'].start()
        
        # Notify about zoom idle status if this is a zoom operation
        if as_name == 'zoom':
            try:
                # Import the interpreter module dynamically to avoid circular imports
                import importlib
                interpreter = importlib.import_module('interpreter')
                if hasattr(interpreter, 'notify_zoom_activity'):
                    interpreter.notify_zoom_activity(False)
                    logging.debug(f"[DEBUG] Notified interpreter that zoom is inactive")
            except Exception as e:
                logging.debug(f"Could not notify interpreter about zoom activity: {e}")

def build_relative_zoom_packet(camid, speed_int):
    """
    Bouwt een relatieve zoom-pakket op basis van de gegeven speed_int.
    speed_int wordt als 16-bit little-endian signed integer verpakt.
    """
    # Zorg ervoor dat speed_int binnen de limieten valt:
    speed_int = max(-32768, min(speed_int, 32767))
    import struct
    lohi = struct.pack('<h', speed_int).hex()
    # Stel het pakket samen (voorbeeld, pas aan op basis van jouw protocol)
    packet = f"0A{camid:02X}06000000098000" + lohi
    return packet



def build_pan_tilt_roll_packet(camid, axis, delta, relative=True):
    """
    Bouwt een hex-pakket voor een relatieve beweging op de gegeven as.
    Dit voorbeeld gaat ervan uit dat het pakket het volgende bevat:
      - Een header: "0A" gevolgd door de camid (2 hex cijfers)
      - Een vast commando "06"
      - Een 6-cijferige vaste waarde "00000E"
      - Een as-specifieke code: "06" voor pan, "07" voor tilt, "08" voor roll
      - Een indicator voor relatieve beweging: "80" voor relatieve commando's
      - "00" als vaste waarde
      - De delta als een 16-bit little-endian signed integer (2 bytes)
    Het resultaat is een hexstring.
    """
    axis_cmd = {'pan': '06', 'tilt': '07', 'roll': '08'}
    if axis not in axis_cmd:
        raise ValueError(f"Onbekende as: {axis}")
    header = "0A" + f"{camid:02X}"
    fixed = "06" + "00000E"
    cmd = axis_cmd[axis]
    # Gebruik "80" voor relatieve beweging
    rel = "80" if relative else "81"
    # Zet delta om naar 16-bit little-endian hex
    delta_int = int(round(delta))
    delta_bytes = struct.pack('<h', delta_int)
    delta_hex = delta_bytes.hex()
    packet = header + fixed + cmd + rel + "00" + delta_hex
    return packet.upper()

def motor_autocalib(apcr, send_apcr_command_func):
    """
    Send a motor auto-calibration command to the APC-R.
    
    Args:
        apcr: APC-R device configuration
        send_apcr_command_func: Function to send commands to the device
    """
    try:
        camid = int(apcr['camid'])
        camid_hex = f"{camid:02x}"
        # Use the correct command for motor calibration
        data = bytes.fromhex("09" + camid_hex + "0400000e0f000000")
        send_apcr_command_func(apcr, data)
        logging.info(f"Motor calibration command sent to {apcr['name']} (CamID: {camid})")
        print(f"Motor calibration command sent to {apcr['name']}")
        return True
    except Exception as e:
        logging.error(f"Failed to send motor calibration command: {e}")
        print(f"[ERROR] Failed to send motor calibration command: {e}")
        return False

def gimbal_autocalib(apcr, send_apcr_command_func):
    """
    Send a gimbal auto-tune command to the APC-R.
    
    Args:
        apcr: APC-R device configuration
        send_apcr_command_func: Function to send commands to the device
    """
    try:
        camid = int(apcr['camid'])
        camid_hex = f"{camid:02x}"
        # Use the correct command for gimbal auto-tuning
        data = bytes.fromhex("09" + camid_hex + "0400000e0e000000")
        send_apcr_command_func(apcr, data)
        logging.info(f"Gimbal auto-tune command sent to {apcr['name']} (CamID: {camid})")
        print(f"Gimbal auto-tune command sent to {apcr['name']}")
        return True
    except Exception as e:
        logging.error(f"Failed to send gimbal auto-tune command: {e}")
        print(f"[ERROR] Failed to send gimbal auto-tune command: {e}")
        return False
    
def get_zoom_packet(percentage, direction, control_type='axis', camid=1):
    """
    Genereer het zoom-UDP-pakket voor in-/uitzoomen, met dynamische camid.
    De 2e byte = camid.

    Let op: De speed-waarde (speed_val) eindigt in 2 bytes. 
    """
    try:
        if direction not in ['in', 'out']:
            print(f"[ERROR] Unknown zoom direction '{direction}'")
            return None

        # Bepaal speed_val
        # Voor 'in' (positief) vs 'out' (negatief). Let op signed vs unsigned.
        if direction == 'in':
            if control_type == 'axis':
                speed_val = 10 * percentage     # as-based
            elif control_type == 'button':
                speed_val = 10 * percentage     # button-based (je kunt dit evt. anders schalen)
            else:
                speed_val = percentage
        else:  # direction == 'out'
            if control_type == 'axis':
                speed_val = -10 * percentage
            elif control_type == 'button':
                speed_val = -10 * percentage
            else:
                speed_val = -percentage

        # Encode de speed_val
        # Zoom 'in' = unsigned short? De firmware van Middlethings kan hier wat eigen logica hebben.
        # In de bestaande code gebruikten we deels <H> (unsigned) en deels <h> (signed).
        # Je kunt hier ook keizen om altijd signed short te gebruiken (struct.pack('<h', speed_val)).
        # Ter illustratie houden we het onderscheid aan:
        if direction == 'in':
            speed_bytes = struct.pack('<H', speed_val)  # unsigned
            logging.debug(f"[DEBUG] Zoom 'in': percentage={percentage}, speed_val={speed_val}, hex={speed_bytes.hex()}")
        else:  # out
            speed_bytes = struct.pack('<h', speed_val)  # signed
            logging.debug(f"[DEBUG] Zoom 'out': percentage={percentage}, speed_val={speed_val}, hex={speed_bytes.hex()}")

        # Packet prefix (2e byte is camid)
        #   0A <camid> 06 00 00 00 09 80 00 ...
        # De laatste 2 bytes worden speed_bytes.
        cam_hex = f"{camid:02x}"
        packet_prefix = bytes.fromhex(f"0A{cam_hex}06000000098000")

        packet = packet_prefix + speed_bytes

        logging.debug(f"[DEBUG] Zoom packet built: {packet.hex()} for percentage: {percentage}, direction: {direction}, control_type: {control_type}")
        return packet

    except Exception as e:
        print(f"[ERROR] Failed to calculate zoom packet: {e}")
        return None


# Predefined zoom levels with corresponding PTR speed values (NOT percentages of current speed)
# These are the actual speed values to use at each zoom level
ZOOM_SPEED_MAPPING = [
    (0, 50),     # At 0% zoom, use ptr_speed of 100%
#    (10, 70),     # At 10% zoom, use ptr_speed of 90%
#    (25, 55),     # At 25% zoom, use ptr_speed of 75%
#    (50, 40),     # At 50% zoom, use ptr_speed of 50%
#    (75, 25),     # At 75% zoom, use ptr_speed of 25%
    (100, 3)     # At 100% zoom, use ptr_speed of 10%
]

def calculate_adaptive_speed(zoom_percentage, base_speed, settings=None, apcr=None):
    """
    Calculate adjusted speed based on the current zoom percentage.
    When adaptive speed is enabled, the base_speed is IGNORED and
    the speed is determined solely by the zoom percentage using
    the mapping values from APC-R specific settings or defaults if not provided.
    
    Args:
        zoom_percentage: Current zoom level as a percentage (0-100)
        base_speed: Base speed from settings (IGNORED when adaptive speed is enabled)
        settings: Settings dictionary to check if adaptive_speed is enabled
        apcr: The APC-R configuration dictionary containing camera-specific settings
        
    Returns:
        int: Adjusted speed (1-100)
    """
    # Check if adaptive speed is enabled (still using global setting for enabling/disabling)
    if settings and "global_settings" in settings:
        adaptive_speed_enabled = settings["global_settings"].get("adaptive_speed", False)
        if not adaptive_speed_enabled:
            return base_speed
    else:
        return base_speed
    
    # Check if we have a valid APC-R configuration
    if not apcr:
        if is_debug_mode():
            print("[DEBUG] No APC-R configuration provided for adaptive speed, using base speed")
        return base_speed
    
    # Ensure zoom_percentage is within valid range (0-100)
    zoom_percentage = max(0, min(100, zoom_percentage))
    
    # Build the mapping from APC-R specific settings or use defaults
    zoom_speed_mapping = []
    
    # Default mapping points
    default_mapping = {
        0: 50,    # At 0% zoom, use ptr_speed of 50%
        100: 3    # At 100% zoom, use ptr_speed of 3%
    }
    
    # Optional mapping points
    optional_zoom_levels = [0, 10, 25, 50, 75, 100]
    
    # Check for custom mapping values in APC-R settings
    for zoom_level in optional_zoom_levels:
        setting_key = f"adaptive_speed_map_{zoom_level}"
        
        if setting_key in apcr and apcr[setting_key] is not None:
            # Use the custom value from APC-R settings
            speed_value = apcr[setting_key]
            zoom_speed_mapping.append((zoom_level, speed_value))
        elif zoom_level in default_mapping:
            # Use the default value for this zoom level
            zoom_speed_mapping.append((zoom_level, default_mapping[zoom_level]))
    
    # Sort the mapping by zoom level
    zoom_speed_mapping.sort(key=lambda x: x[0])
    
    # Make sure we have at least two points for interpolation
    if len(zoom_speed_mapping) < 2:
        if is_debug_mode():
            print(f"[DEBUG] Invalid adaptive speed mapping: {zoom_speed_mapping}. Using base speed.")
        return base_speed
    
    # Find the appropriate speed adjustment based on the current zoom level
    # Linear interpolation between defined points
    for i in range(len(zoom_speed_mapping) - 1):
        lower_zoom, lower_speed = zoom_speed_mapping[i]
        upper_zoom, upper_speed = zoom_speed_mapping[i + 1]
        
        if lower_zoom <= zoom_percentage <= upper_zoom:
            # Calculate percentage within this range
            range_percentage = (zoom_percentage - lower_zoom) / (upper_zoom - lower_zoom)
            # Linear interpolation to get absolute speed value (not a scaling factor)
            adjusted_speed = int(lower_speed + range_percentage * (upper_speed - lower_speed))
            # Ensure it's in valid range (1-100)
            adjusted_speed = max(1, min(100, adjusted_speed))
            
            if is_debug_mode():
                print(f"[DEBUG] Adaptive Speed for {apcr.get('name', 'Unknown')}: zoom={zoom_percentage}%, " +
                      f"calculated_speed={adjusted_speed}% (between {lower_speed}% at {lower_zoom}% and {upper_speed}% at {upper_zoom}%)")
                
            return adjusted_speed
    
    # If we get here, the zoom_percentage is outside the range of our mapping
    # Use the nearest endpoint
    if zoom_percentage < zoom_speed_mapping[0][0]:
        return zoom_speed_mapping[0][1]
    else:
        return zoom_speed_mapping[-1][1]



def get_current_zoom_percentage(apcr):
    """
    Get the current zoom percentage from the current position.
    
    Args:
        apcr: APC-R configuration dictionary
        
    Returns:
        float: Zoom percentage (0-100) or None if not available
    """
    pos_str = get_current_position(apcr)
    if pos_str:
        try:
            parts = pos_str.strip().split(';')
            if len(parts) >= 6:
                zoom_value = float(parts[5])
                # Convert from absolute zoom value (1-4095) to percentage (0-100)
                zoom_percentage = (zoom_value - 1) * 100 / 4094
                return zoom_percentage
        except Exception as e:
            logging.error(f"[ERROR] Failed to parse zoom value: {e}")
    
    return None


def toggle_adaptive_speed(apcr, settings, save_settings_func):
    """
    Toggle adaptive speed on/off.
    
    Args:
        apcr: Active APC-R configuration
        settings: Settings dictionary
        save_settings_func: Function to save settings
        
    Returns:
        str: Status message
    """
    if "global_settings" not in settings:
        print("[ERROR] global_settings not found in settings")
        return "ERROR: NO_GLOBAL_SETTINGS"
    
    # Toggle de huidige status
    current_state = settings["global_settings"].get("adaptive_speed", False)
    new_state = not current_state
    
    # Update settings
    settings["global_settings"]["adaptive_speed"] = new_state
    
    # Save settings
    save_settings_func(settings)
    
    # Log en return status
    status = "ON" if new_state else "OFF"
    print(f"Adaptive speed is now {status}")
    return f"ADAPTIVE_SPEED: {status}"

