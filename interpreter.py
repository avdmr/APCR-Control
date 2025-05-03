import socketserver
import threading
import time
import logging
import json
import os
import socket
import presets
import controls
import re

active_handlers = []
_zoom_active = False
_global_position_cache = {} 

# Zorg dat PRESETS_FILE een string is
PRESETS_FILE = "presets.json"

# ObservableSettings: een wrapper rond een dictionary
class ObservableSettings(dict):
    def __init__(self, *args, on_change=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_change = on_change

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        # Als een kritieke sleutel verandert, roep de callback aan
        if key in ["zoom_speed", "ptr_speed", "preset_transition_speed"]:
            if self.on_change:
                self.on_change()

def trigger_status_update():
    """
    Trigger a status update for all active handlers.
    This function is called when settings change or when we need to force an update.
    """
    for handler in active_handlers:
        try:
            handler.send_status_packet()
        except Exception as e:
            logging.error(f"Error triggering status update for a handler: {e}")

def get_position_values(apcr, use_cache=True):
    """
    Haalt de huidige positie op van de camera via controls.get_current_position.
    Als use_cache=True en er is geen verse data, gebruik dan de cache.
    """
    pos_str = controls.get_current_position(apcr)
    if pos_str:
        parts = pos_str.strip().split(';')
        if len(parts) >= 6:
            try:
                pan = float(parts[2])
                tilt = float(parts[3])
                roll = float(parts[4])
                zoom_val = float(parts[5])
                # Bereken zoompercentage
                zoom_percentage = (zoom_val - 1) * 100 / 4094
                position_data = {
                    'pan': pan,
                    'tilt': tilt,
                    'roll': roll,
                    'zoom': int(round(zoom_percentage))
                }
                # Update de globale cache direct hier
                if '_global_position_cache' in globals():
                    _global_position_cache[apcr.get('camid')] = position_data.copy()
                return position_data
            except Exception as e:
                logging.error(f"Error computing position values: {e}")
    
    # Als we geen data hebben maar use_cache is True, gebruik de cache
    if use_cache and '_global_position_cache' in globals():
        return _global_position_cache.get(apcr.get('camid'))
    
    return None


def notify_zoom_activity(is_active):
    """
    Called when zoom activity state changes.
    Triggers a status update to Companion without affecting camera movement.
    
    Args:
        is_active: True if zoom is active, False if zoom stops
    """
    global _zoom_active
    
    # Only do something if the state changes
    if _zoom_active != is_active:
        _zoom_active = is_active
        
        # Just send a status update to Companion - no delays or camera commands
        trigger_status_update()
        
        logging.debug(f"Zoom activity changed to: {'active' if is_active else 'inactive'}")

class PresetsCache:
    """
    Cache for presets data to avoid frequent disk reads.
    """
    def __init__(self):
        self.cache = {}
        self.last_mtime = None

    def get_presets(self, camid):
        mtime = os.path.getmtime(PRESETS_FILE)
        if self.last_mtime == mtime and camid in self.cache:
            return self.cache[camid]
        
        # Herlaad presets
        presets_data = presets.list_presets(camid)
        self.cache[camid] = presets_data
        self.last_mtime = mtime
        return presets_data
    
    def invalidate(self, camid=None):
        if camid is None:
            self.cache = {}
        elif camid in self.cache:
            del self.cache[camid]

presets_cache = PresetsCache()

class MiddlethingsHandler(socketserver.StreamRequestHandler):
    """
    Handler for TCP connections from Bitfocus Companion.
    Processes incoming commands and sends status updates.
    """
    
    # Class attributes shared across all instances
    settings = None
    send_apcr_command_func = None
    get_active_apcr_func = None
    save_settings_func = None
    write_lock = threading.Lock()

    def setup(self):
        super().setup()
        self.stop_event = threading.Event()
        self.connected = False
        active_handlers.append(self)
        
        try:
            self.request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 128 * 1024)
            self.request.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 128 * 1024)
        except OSError:
            # Fallback 
            try:
                self.request.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 64 * 1024)
                self.request.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 64 * 1024)
            except OSError:
                logging.warning("Could not increase socket buffer size")

        self.request.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        
        # Voor command throttling
        self._command_in_progress = threading.Event()
        self._last_command_time = 0
        self._last_known_positions = {}

    def finish(self):
        if self in active_handlers:
            active_handlers.remove(self)
        super().finish()


    def send_status_packet(self):
        try:
            active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
            if active_apcr:
                camid = active_apcr.get('camid', 0)
                gs = self.settings["global_settings"]
                
                # Determine if we should show effective speed or base speed
                if gs.get("adaptive_speed", False):
                    # Get the effective PTR speed when adaptive speed is enabled
                    ptr_speed = self.get_effective_ptr_speed(active_apcr)
                else:
                    # Use the base PTR speed from settings
                    ptr_speed = gs.get("ptr_speed", 100)
                    
                zoom_speed = gs.get("zoom_speed", 100)
                preset_speed = gs.get("preset_transition_speed", 100)
                adaptive_speed = "1" if gs.get("adaptive_speed", False) else "0"
                
                # Get virtual wall status
                virtualwall = "1" if gs.get("virtualwall", False) else "0"
                virtualwallpreset = "1" if gs.get("virtualwallpreset", False) else "0"

                # Haal positiegegevens op
                pos = get_position_values(active_apcr)
                if pos:
                    # Update onze cache met de nieuwe waarden
                    self._last_known_positions[camid] = pos.copy()
                else:
                    # Gebruik de laatst bekende waarden als beschikbaar
                    pos = self._last_known_positions.get(camid)
                if pos:
                    # Build packet with camera-specific virtual wall settings
                    has_pan_wall = (active_apcr.get("virtualwallstart_pan") is not None and 
                                    active_apcr.get("virtualwallend_pan") is not None)
                    has_tilt_wall = (active_apcr.get("virtualwallstart_tilt") is not None and 
                                    active_apcr.get("virtualwallend_tilt") is not None)
                    
                    status_packet = (
                        f"{{CAM{camid};PTS{ptr_speed};ZS{zoom_speed};PRES_D{preset_speed};"
                        f"aPAN{pos['pan']};aTILT{pos['tilt']};aROLL{pos['roll']};"
                        f"aZOOM{pos['zoom']};ADAPT{adaptive_speed};"
                        f"VWALL{virtualwall};VWALLPRES{virtualwallpreset};"
                        f"HASVWPAN{1 if has_pan_wall else 0};"
                        f"HASVWTILT{1 if has_tilt_wall else 0};}}"
                    )
                    self.safe_write(status_packet)
                else:
                    status_packet = f"{{CAM{camid};PTS{ptr_speed};ZS{zoom_speed};PRES_D{preset_speed};ADAPT{adaptive_speed};}}"
            else:
                status_packet = "{CAM0;PTS0;ZS0;SS0;ADAPT0;}"
            self.safe_write(status_packet)
        except Exception as e:
            logging.error(f"Immediate status update error: {e}")




    def send_status_loop(self):
        while not self.stop_event.is_set():
            try:
                # Nieuwe code: check of er een commando actief is
                if self._command_in_progress.is_set():
                    time.sleep(0.1)
                    continue
                    
                active_apcr = None
                if MiddlethingsHandler.get_active_apcr_func:
                    active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
                if not active_apcr:
                    status_packet = "{CAM0;PTS0;ZS0;SS0;ADAPT0;}"
                else:
                    camid = active_apcr.get('camid', 0)
                    gs = self.settings["global_settings"]
                    
                    # Determine if we should show effective speed or base speed
                    if gs.get("adaptive_speed", False):
                        # Get the effective PTR speed when adaptive speed is enabled
                        ptr_speed = self.get_effective_ptr_speed(active_apcr)
                    else:
                        # Use the base PTR speed from settings
                        ptr_speed = gs.get("ptr_speed", 100)
                        
                    zoom_speed = gs.get("zoom_speed", 100)
                    preset_speed = gs.get("preset_transition_speed", 100)
                    adaptive_speed = "1" if gs.get("adaptive_speed", False) else "0"
                    pos = get_position_values(active_apcr)
                    if pos:
                        status_packet = (
                            f"{{CAM{camid};PTS{ptr_speed};ZS{zoom_speed};PRES_D{preset_speed};"
                            f"aPAN{pos['pan']};aTILT{pos['tilt']};aROLL{pos['roll']};"
                            f"aZOOM{pos['zoom']};ADAPT{adaptive_speed};}}"
                        )
                    else:
                        status_packet = f"{{CAM{camid};PTS{ptr_speed};ZS{zoom_speed};PRES_D{preset_speed};ADAPT{adaptive_speed};}}"
                    # Indien er ook presets zijn, kun je die (zoals voorheen) toevoegen aan het pakket
                    presets_list = presets_cache.get_presets(camid)
                    for key in presets_list:
                        if "." in key:
                            _, slot_num = key.split(".")
                            if slot_num.isdigit():
                                try:
                                    slot = int(slot_num)
                                    status_packet = status_packet.rstrip() + f"PRES_D{slot};PRES_C{slot};"
                                except ValueError:
                                    pass
                    #status_packet = status_packet.rstrip("\n") + "\n"
                    
                self.safe_write(status_packet)
                if MiddlethingsHandler.settings and MiddlethingsHandler.settings.get("debug_mode", False):
                    print(f"[DEBUG] Sent status packet: {status_packet.strip()}")
            except Exception as e:
                logging.error(f"Error sending status packet: {e}")
                self.connected = False
                break
            time.sleep(1)

    def send_chunked_status(self, status_packet):
        """Send large status packets in chunks to prevent buffer overflow."""
        CHUNK_SIZE = 4096  # 4KB chunks
        
        try:
            encoded_packet = status_packet.encode('utf-8')
            
            if len(encoded_packet) <= CHUNK_SIZE:
                # No split needed
                with MiddlethingsHandler.write_lock:
                    self.wfile.write(encoded_packet)
                    self.wfile.flush()
            else:
                # Splits in chunks
                with MiddlethingsHandler.write_lock:
                    for i in range(0, len(encoded_packet), CHUNK_SIZE):
                        chunk = encoded_packet[i:i+CHUNK_SIZE]
                        self.wfile.write(chunk)
                        self.wfile.flush()
                        time.sleep(0.01) # pause between chunks
        except Exception as e:
            logging.error(f"Error sending chunked status: {e}")
            raise


    def handle(self):
        client_ip = self.client_address[0]
        print(f"Bitfocus Companion connected from {client_ip}")

        try:
            # ---------- minimale handshake ----------
            HANDSHAKE = [
                "STATUS: disconnected - null",
                "STATUS: connecting - null",
                "STATUS: ok - null",
            ]
            for i, msg in enumerate(HANDSHAKE):
                self.safe_write(msg)        # LF‑afsluiting
                if i < len(HANDSHAKE) - 1:  # 2 × 70 ms ≈ 140 ms totaal
                    time.sleep(0.07)

            # 100 ms rust zodat buffer leeg is vóór eerste status‑pakket
            time.sleep(0.10)

            # ---------- status‑thread starten ----------
            self.connected = True
            self.stop_event.clear()
            self.status_thread = threading.Thread(
                target=self.send_status_loop, daemon=True
            )
            self.status_thread.start()

            # ---------- command‑loop ----------
            while self.connected:
                data = self.rfile.readline()
                if not data:
                    print("No data received from Companion, connection broken")
                    break

                message = data.decode("utf-8").strip()
                print(f"Received from Companion: {message}")

                response = self.process_command(message)
                if response:
                    self.safe_write(response)

        except Exception as e:
            print(f"General error in connection function: {e}")

        finally:
            self.stop_event.set()
            self.connected = False
            print(f"Connection with {client_ip} broken")



    def safe_write(self, text: str):
        """Thread‑safe write met gegarandeerde LF‐afsluiting (geen CR)."""
        if not text.endswith("\n"):
            text = text.rstrip("\r\n") + "\n"     
        data = text.encode("utf-8")

        with MiddlethingsHandler.write_lock:
            self.wfile.write(data)
            self.wfile.flush()

    # -------------------------------
    # Nieuwe mapping van Companion-commando's
    # -------------------------------


# Then modify the process_command method to handle these new command formats:
    def process_command(self, command):
        """
        Process incoming commands from Companion using a mapping dictionary.
        """
        self._command_in_progress.set()
        self._last_command_time = time.time()
        
        cmd = command.strip().upper()

        # Handle camera selection commands (CAM1, CAM2, etc.)
        if cmd.startswith("CAM"):
            num_part = cmd[3:]
            if num_part.isdigit():
                result = self.handle_camera_selection(num_part)
            
                threading.Timer(0.2, self._command_in_progress.clear).start()
                return result
                    
        # Handle preset recall commands (PRESET1C2, PRESET5C1, etc.)
        preset_recall_match = re.match(r'PRESET(\d+)C(\d+)', cmd)
        if preset_recall_match:
            slot_id = preset_recall_match.group(1)
            cam_id = preset_recall_match.group(2)
            return self.handle_preset_recall(cam_id, slot_id)
            
        # Handle preset save commands (SPRESET1C2, SPRESET5C1, etc.)
        preset_save_match = re.match(r'SPRESET(\d+)C(\d+)', cmd)
        if preset_save_match:
            slot_id = preset_save_match.group(1)
            cam_id = preset_save_match.group(2)
            return self.handle_preset_save(cam_id, slot_id)
        
        #Handle transition speed commands (PRES_D50, etc.)
        transition_speed_match = re.match(r'PRES_D(\d+)', cmd)
        if transition_speed_match:
            speed_value = transition_speed_match.group(1)
            return self.handle_transition_speed(speed_value)        

        
        # Standard command mapping for other functions
        companion_mapping = {
            "PAN_L": self.handle_pan_left,
            "PAN_R": self.handle_pan_right,
            "PAN_IDLE": self.handle_pan_idle,
            "TILT_U": self.handle_tilt_up,
            "TILT_D": self.handle_tilt_down,
            "TILT_IDLE": self.handle_tilt_idle,
            "ROLL_L": self.handle_roll_left,
            "ROLL_R": self.handle_roll_right,
            "ROLL_IDLE": self.handle_roll_idle,
            "ZOOM+": self.handle_zoom_in,
            "ZOOM-": self.handle_zoom_out,
            "Z0": self.handle_zoom_idle,
            "ZSPEED+": self.handle_zspeed_plus,
            "ZSPEED-": self.handle_zspeed_minus,
            "SPEED+": self.handle_speed_plus,
            "SPEED-": self.handle_speed_minus,
            "ACTIVETRACK": self.handle_active_track,
            "GIMBALAUTOCALIB": self.handle_gimbal_autocalib,
            "MOTORAUTOCALIB": self.handle_motor_autocalib,
            "RECENTER": self.handle_recenter,
            "ZSSHORTCUT": self.handle_zs_shortcut,
            "PTSSHORTCUT": self.handle_pts_shortcut,
            "ADAPTIVESPEED":self.handle_adaptive_speed,
        }
        if cmd in companion_mapping:
            # WIJZIG DIT DEEL:
            # Van: return companion_mapping[cmd]()
            # Naar:
            def execute_command():
                try:
                    result = companion_mapping[cmd]()
                    self.safe_write(result)
                except Exception as e:
                    logging.error(f"Error executing command {cmd}: {e}")
                finally:
                    threading.Timer(0.2, self._command_in_progress.clear).start()
            
            # Start commando in aparte thread
            threading.Thread(target=execute_command, daemon=True).start()
            return "COMMAND: ACK"  # Direct bevestigen
        else:
            # NIEUWE CODE: Reset ook hier
            threading.Timer(0.2, self._command_in_progress.clear).start()
            return "ERROR: UNKNOWN_COMMAND"

    def get_effective_ptr_speed(self, apcr):
        """
        Get the effective PTR speed that's currently being applied.
        If adaptive speed is enabled, this might be different from the stored value.
        
        Args:
            apcr: The active APC-R configuration
            
        Returns:
            int: The effective PTR speed (1-100)
        """
        gs = self.settings["global_settings"]
        base_speed = gs.get("ptr_speed", 100)
        
        if not gs.get("adaptive_speed", False):
            return base_speed
        
        # If adaptive speed is enabled, calculate the effective speed
        try:
            zoom_percentage = None
            pos_str = controls.get_current_position(apcr)
            if pos_str:
                parts = pos_str.strip().split(';')
                if len(parts) >= 6:
                    zoom_value = float(parts[5])
                    # Convert from absolute zoom value (1-4095) to percentage (0-100)
                    zoom_percentage = (zoom_value - 1) * 100 / 4094
                    
            if zoom_percentage is not None:
                # Calculate the adaptive speed that's being applied
                return controls.calculate_adaptive_speed(zoom_percentage, base_speed, self.settings, apcr)  # Pass apcr parameter
        except Exception as e:
            logging.error(f"Error calculating effective PTR speed: {e}")
        
        # Fallback to base speed if we couldn't calculate the adaptive speed
        return base_speed

    
    def handle_adaptive_speed(self):
        """
        Toggle adaptive speed setting when the ADAPTIVESPEED command is received from Companion.
        
        Returns:
            str: Response message
        """
        if not self.settings or "global_settings" not in self.settings:
            return "ERROR: NO_SETTINGS_AVAILABLE"
        
        # Toggle de huidige status
        current_state = self.settings["global_settings"].get("adaptive_speed", False)
        new_state = not current_state
        
        # Update de instelling
        self.settings["global_settings"]["adaptive_speed"] = new_state
        
        # Save de settings
        if MiddlethingsHandler.save_settings_func:
            MiddlethingsHandler.save_settings_func(self.settings)
        
        # Trigger een update naar alle clients
        trigger_status_update()
        
        status = "ON" if new_state else "OFF"
        print(f"Adaptive speed is now {status}")
        return f"ADAPTIVESPEED: {status}"
    


    # Add these two new handler methods:
    def handle_preset_recall(self, cam_id, slot_id):
        """
        Handle preset recall command from Companion.
        Format: PRESET[slot]C[camid]
        
        Args:
            cam_id: Camera ID
            slot_id: Preset slot ID
        
        Returns:
            str: Response message
        """
        try:
            # Convert to integers
            cam_id_int = int(cam_id)
            slot_id_int = int(slot_id)
            
            # Find the specified camera
            target_apcr = None
            for apcr in self.settings.get("apcrs", []):
                if apcr.get("camid") == cam_id_int:
                    target_apcr = apcr
                    break
            
            if not target_apcr:
                return f"ERROR: CAMERA {cam_id} NOT FOUND"
            
            # Recall the preset
            presets.recall_preset(cam_id_int, slot_id_int, target_apcr, self.settings)
            threading.Timer(0.05, self.send_status_packet).start()  
            return f"PRESET {slot_id}C{cam_id} RECALLED"
        except Exception as e:
            logging.error(f"Error recalling preset: {e}")
            return f"ERROR: PRESET_RECALL_FAILED"

    def handle_preset_save(self, cam_id, slot_id):
        """
        Handle preset save command from Companion.
        Format: SPRESET[slot]C[camid]
        
        Args:
            cam_id: Camera ID
            slot_id: Preset slot ID
        
        Returns:
            str: Response message
        """
        try:
            # Convert to integers
            cam_id_int = int(cam_id)
            slot_id_int = int(slot_id)
            
            # Find the specified camera
            target_apcr = None
            for apcr in self.settings.get("apcrs", []):
                if apcr.get("camid") == cam_id_int:
                    target_apcr = apcr
                    break
            
            if not target_apcr:
                return f"ERROR: CAMERA {cam_id} NOT FOUND"
            
            # Get the current position
            pos_str = controls.get_current_position(target_apcr)
            if not pos_str:
                return "ERROR: CURRENT_POSITION_NOT_AVAILABLE"
            
            # Parse position data
            parts = pos_str.strip().split(';')
            if len(parts) < 6:
                return "ERROR: INVALID_POSITION_DATA"
            
            # Create position dictionary
            position = {
                'pan': float(parts[2]),
                'tilt': float(parts[3]),
                'roll': float(parts[4]),
                'zoom': float(parts[5])
            }
            
            # Save the preset
            success = presets.save_preset(cam_id_int, slot_id_int, position)
            if success:
                # Invalidate cache to ensure status updates show the new preset
                presets_cache.invalidate(cam_id_int)
                trigger_status_update()
                return f"SPRESET {slot_id}C{cam_id} SAVED"
            else:
                return "ERROR: PRESET_SAVE_FAILED"
        except Exception as e:
            logging.error(f"Error saving preset: {e}")
            return f"ERROR: PRESET_SAVE_FAILED"

    def handle_transition_speed(self, speed_value):
        """
        Handle preset transition speed adjustment command from Companion.
        Format: PRES_D[value]
        
        Args:
            speed_value: Speed value (1-100)
        
        Returns:
            str: Response message
        """
        try:
            value = int(speed_value)
            if 1 <= value <= 100:
                self.settings["global_settings"]["preset_transition_speed"] = value
                
                # Save the settings
                if MiddlethingsHandler.save_settings_func:
                    MiddlethingsHandler.save_settings_func(self.settings)
                    
                # Trigger status update for all connected interfaces
                trigger_status_update()
                
                return f"TRANSITION_SPEED: {value}%"
            else:
                return "ERROR: SPEED_VALUE_OUT_OF_RANGE (1-100)"
        except ValueError:
            return "ERROR: INVALID_SPEED_VALUE"


    def handle_pan_left(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        gs = self.settings["global_settings"]
        speed = gs.get("ptr_speed", 100)
        controls.start_or_update_movement(
            as_name="pan",
            direction="negative",
            percentage=speed,
            send_apcr_command=MiddlethingsHandler.send_apcr_command_func,
            apcr=active_apcr,
            control_type='button',
            settings=self.settings
        )
        threading.Timer(0.05, self.send_status_packet).start()
        return f"PAN_L: CamID {active_apcr['camid']} ({active_apcr['name']})"

    def handle_pan_right(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        gs = self.settings["global_settings"]
        speed = gs.get("ptr_speed", 100)
        controls.start_or_update_movement(
            as_name="pan",
            direction="positive",
            percentage=speed,
            send_apcr_command=MiddlethingsHandler.send_apcr_command_func,
            apcr=active_apcr,
            control_type='button',
            settings=self.settings
        )
        threading.Timer(0.05, self.send_status_packet).start()
        return f"PAN_R: CamID {active_apcr['camid']} ({active_apcr['name']})"

    def handle_pan_idle(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if active_apcr:
            controls.stop_movement("pan", MiddlethingsHandler.send_apcr_command_func, active_apcr)
            threading.Timer(0.05, self.send_status_packet).start()
            return f"PAN_IDLE: CamID {active_apcr['camid']} ({active_apcr['name']})"
        else:
            return "ERROR: NO_ACTIVE_CAMERA"
        
    def handle_tilt_up(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        gs = self.settings["global_settings"]
        speed = gs.get("ptr_speed", 100)
        controls.start_or_update_movement(
            as_name="tilt",
            direction="negative",
            percentage=speed,
            send_apcr_command=MiddlethingsHandler.send_apcr_command_func,
            apcr=active_apcr,
            control_type='button',
            settings=self.settings
        )
        threading.Timer(0.05, self.send_status_packet).start()
        return "COMMAND: ACK"

    def handle_tilt_down(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        gs = self.settings["global_settings"]
        speed = gs.get("ptr_speed", 100)
        controls.start_or_update_movement(
            as_name="tilt",
            direction="positive",
            percentage=speed,
            send_apcr_command=MiddlethingsHandler.send_apcr_command_func,
            apcr=active_apcr,
            control_type='button',
            settings=self.settings
        )
        threading.Timer(0.05, self.send_status_packet).start()
        return "COMMAND: ACK"

    def handle_tilt_idle(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if active_apcr:
            threading.Timer(0.05, self.send_status_packet).start()
            controls.stop_movement("tilt", MiddlethingsHandler.send_apcr_command_func, active_apcr)
            return "COMMAND: ACK"
        else:
            return "ERROR: NO_ACTIVE_CAMERA"


    def handle_roll_left(self):
        """
        Handle the roll left command from Companion.
        """
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        gs = self.settings["global_settings"]
        speed = gs.get("ptr_speed", 100)  # Roll gebruikt dezelfde snelheid als pan/tilt
        controls.start_or_update_movement(
            as_name="roll",
            direction="negative",
            percentage=speed,
            send_apcr_command=MiddlethingsHandler.send_apcr_command_func,
            apcr=active_apcr,
            control_type='button',
            settings=self.settings
        )
        threading.Timer(0.05, self.send_status_packet).start()
        return f"ROLL_L: CamID {active_apcr['camid']} ({active_apcr['name']})"

    def handle_roll_right(self):
        """
        Handle the roll right command from Companion.
        """
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        gs = self.settings["global_settings"]
        speed = gs.get("ptr_speed", 100)  # Roll gebruikt dezelfde snelheid als pan/tilt
        controls.start_or_update_movement(
            as_name="roll",
            direction="positive",
            percentage=speed,
            send_apcr_command=MiddlethingsHandler.send_apcr_command_func,
            apcr=active_apcr,
            control_type='button',
            settings=self.settings
        )
        threading.Timer(0.05, self.send_status_packet).start()
        return f"ROLL_R: CamID {active_apcr['camid']} ({active_apcr['name']})"

    def handle_roll_idle(self):
        """
        Handle the roll idle command from Companion.
        """
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if active_apcr:
            controls.stop_movement("roll", MiddlethingsHandler.send_apcr_command_func, active_apcr)
            threading.Timer(0.05, self.send_status_packet).start()
            return f"ROLL_IDLE: CamID {active_apcr['camid']} ({active_apcr['name']})"
        else:
            return "ERROR: NO_ACTIVE_CAMERA"

    def handle_movement_command(self, as_name, direction, percentage):
        """Helper functie voor bewegingscommando's"""
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        
        # Verhoog tijdelijk de positie request frequentie
        original_freq = self.settings["global_settings"].get("position_request_frequency", 1.2)
        self.settings["global_settings"]["position_request_frequency"] = 0.1
        
        # Start beweging
        controls.start_or_update_movement(
            as_name=as_name,
            direction=direction,
            percentage=percentage,
            send_apcr_command=MiddlethingsHandler.send_apcr_command_func,
            apcr=active_apcr,
            control_type='button',
            settings=self.settings
        )
        
        # Herstel oorspronkelijke frequentie na 1 seconde
        def restore_frequency():
            self.settings["global_settings"]["position_request_frequency"] = original_freq
        
        threading.Timer(1.0, restore_frequency).start()
        
        # Stuur status update na korte vertraging
        threading.Timer(0.05, self.send_status_packet).start()
        
        return f"{as_name.upper()}_{direction.upper()}: CamID {active_apcr['camid']}"
        
    def handle_zoom_in(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        gs = self.settings["global_settings"]
        speed = gs.get("zoom_speed", 100)
        
        # Stuur eerst een directe status update voordat de zoom begint
        # Dit zorgt voor snellere updates bij Companion-geïnitieerde zoom acties
        notify_zoom_activity(True)
        
        # Verstuur het zoom commando naar de camera
        controls.start_or_update_movement(
            as_name="zoom",
            direction="in",
            percentage=speed,
            send_apcr_command=MiddlethingsHandler.send_apcr_command_func,
            apcr=active_apcr,
            control_type='button',
            settings=self.settings
        )
        
        # Start een korte timer voor een tweede update vlak na het begin van de zoom
        # Dit verbetert de responsiviteit van de Companion interface
        threading.Timer(0.05, self.send_status_packet).start()
        return "COMMAND: ACK"

    def handle_zoom_out(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        gs = self.settings["global_settings"]
        speed = gs.get("zoom_speed", 100)
        
        # Stuur eerst een directe status update voordat de zoom begint
        # Dit zorgt voor snellere updates bij Companion-geïnitieerde zoom acties
        notify_zoom_activity(True)
        
        # Verstuur het zoom commando naar de camera
        controls.start_or_update_movement(
            as_name="zoom",
            direction="out",
            percentage=speed,
            send_apcr_command=MiddlethingsHandler.send_apcr_command_func,
            apcr=active_apcr,
            control_type='button',
            settings=self.settings
        )
        
        # Start een korte timer voor een tweede update vlak na het begin van de zoom
        # Dit verbetert de responsiviteit van de Companion interface
        threading.Timer(0.05, self.send_status_packet).start()
        return "COMMAND: ACK"

    def handle_zoom_idle(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if active_apcr:
            # Stuur eerst een directe status update voordat de zoom stopt
            # Dit zorgt voor snellere updates bij Companion-geïnitieerde zoom stops
            notify_zoom_activity(False)
            
            # Stop de zoom beweging
            controls.stop_movement("zoom", MiddlethingsHandler.send_apcr_command_func, active_apcr)
            
            # Start een korte timer voor een tweede update vlak na het stoppen van de zoom
            # Dit verbetert de responsiviteit van de Companion interface
            threading.Timer(0.05, self.send_status_packet).start()
            return "COMMAND: ACK"
        else:
            return "ERROR: NO_ACTIVE_CAMERA"
        
    def handle_zspeed_plus(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
            
        gs = self.settings["global_settings"]
        
        # Verwijderd: code om adaptive speed uit te schakelen
        
        # Increment the speed (max 100)
        gs["zoom_speed"] = min(gs.get("zoom_speed", 100) + 5, 100)
        
        # Save settings
        if MiddlethingsHandler.save_settings_func:
            MiddlethingsHandler.save_settings_func(self.settings)
        
        trigger_status_update()
        return "COMMAND: ACK"

    def handle_zspeed_minus(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
            
        gs = self.settings["global_settings"]
        
        # Verwijderd: code om adaptive speed uit te schakelen
        
        # Decrement the speed (min 1)
        gs["zoom_speed"] = max(gs.get("zoom_speed", 100) - 5, 1)
        
        # Save settings
        if MiddlethingsHandler.save_settings_func:
            MiddlethingsHandler.save_settings_func(self.settings)
        
        trigger_status_update()
        return "COMMAND: ACK"
    
    def handle_speed_plus(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
            
        gs = self.settings["global_settings"]
        
        # Get the effective speed (the one that's currently being applied)
        effective_speed = self.get_effective_ptr_speed(active_apcr)
        
        # If adaptive speed is enabled, disable it and use the effective speed as the new base
        if gs.get("adaptive_speed", False):
            gs["adaptive_speed"] = False
            gs["ptr_speed"] = effective_speed
            logging.info(f"Adaptive speed disabled by speed+ command. PTR speed set to effective value: {effective_speed}%")
        
        # Increment the speed (max 100)
        gs["ptr_speed"] = min(gs.get("ptr_speed", 100) + 5, 100)
        
        # Save settings
        if MiddlethingsHandler.save_settings_func:
            MiddlethingsHandler.save_settings_func(self.settings)
        
        trigger_status_update()
        return "COMMAND: ACK"

    def handle_speed_minus(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
            
        gs = self.settings["global_settings"]
        
        # Get the effective speed (the one that's currently being applied)
        effective_speed = self.get_effective_ptr_speed(active_apcr)
        
        # If adaptive speed is enabled, disable it and use the effective speed as the new base
        if gs.get("adaptive_speed", False):
            gs["adaptive_speed"] = False
            gs["ptr_speed"] = effective_speed
            logging.info(f"Adaptive speed disabled by speed- command. PTR speed set to effective value: {effective_speed}%")
        
        # Decrement the speed (min 1)
        gs["ptr_speed"] = max(gs.get("ptr_speed", 100) - 5, 1)
        
        # Save settings
        if MiddlethingsHandler.save_settings_func:
            MiddlethingsHandler.save_settings_func(self.settings)
        
        trigger_status_update()
        return "COMMAND: ACK"

    def handle_active_track(self):
        """
        Toggle de active_track status in de settings (voor de actieve APC-R).
        Stuur het active track pakketje en print de status.
        """
        active_apcr = MiddlethingsHandler.get_active_apcr_func(self.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        
        # Gebruik de camid en stel het pakket samen
        camid = active_apcr.get('camid', 1)
        data = bytes.fromhex("08" + f"{camid:02x}" + "0400000e0b0000")
        
        # Toggle de active_track status in de apcr settings
        active_apcr['active_track'] = not active_apcr.get('active_track', False)
        
        # Print de nieuwe status naar de console
        if active_apcr['active_track']:
            print("Active track ON")
            # Start de active track monitor indien nodig
            if hasattr(controls, 'start_active_track_monitor'):
                if (not hasattr(controls, '_active_track_monitor_thread') or 
                    controls._active_track_monitor_thread is None or 
                    not controls._active_track_monitor_thread.is_alive()):
                    controls.start_active_track_monitor(
                        self.settings, 
                        MiddlethingsHandler.send_apcr_command_func, 
                        MiddlethingsHandler.save_settings_func
                    )
        else:
            print("Active track OFF")
            # Controleer of er nog andere camera's zijn met active track aan
            any_active = False
            for apcr in self.settings.get("apcrs", []):
                if apcr.get('active_track', False) and apcr != active_apcr:
                    any_active = True
                    break
            
            # Stop monitor als geen enkele camera active track aan heeft
            if not any_active and hasattr(controls, 'stop_active_track_monitor'):
                controls.stop_active_track_monitor()
        
        # Stuur het pakketje naar de APC-R
        MiddlethingsHandler.send_apcr_command_func(active_apcr, data)
        
        # Sla de gewijzigde settings op (als een save functie is gekoppeld)
        if MiddlethingsHandler.save_settings_func:
            MiddlethingsHandler.save_settings_func(self.settings)
        
        # Trigger een status update zodat de Companion direct de wijziging ziet
        trigger_status_update()
        
        return f"ACTIVE_TRACK set to {active_apcr['active_track']}"


    def handle_gimbal_autocalib(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        
        if hasattr(controls, "gimbal_autocalib"):
            try:
                result = controls.gimbal_autocalib(active_apcr, MiddlethingsHandler.send_apcr_command_func)
                if result:
                    return "COMMAND: ACK"
                else:
                    return "ERROR: GIMBAL_CALIBRATION_FAILED"
            except Exception as e:
                logging.error(f"Error in gimbal_autocalib: {e}")
                return f"ERROR: GIMBAL_CALIBRATION_EXCEPTION: {str(e)}"
        else:
            logging.error("gimbal_autocalib function not found in controls module")
            return "ERROR: FUNCTION_NOT_IMPLEMENTED"
        
    def handle_motor_autocalib(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        if hasattr(controls, "motor_autocalib"):
            controls.motor_autocalib(active_apcr, MiddlethingsHandler.send_apcr_command_func)
            return "COMMAND: ACK"
        else:
            return "ERROR: FUNCTION_NOT_IMPLEMENTED"

    def handle_recenter(self):
        active_apcr = MiddlethingsHandler.get_active_apcr_func(self.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
        controls.send_recenter(active_apcr, MiddlethingsHandler.send_apcr_command_func)
        return f"RECENTER: CamID {active_apcr['camid']} ({active_apcr['name']})"

    def handle_pts_shortcut(self):
        """
        Elastische shortcut voor ptr_speed (pan/tilt speed):
        100 -> 75 -> 50 -> 25 -> 5 -> 1 en dan weer omhoog.
        Bij adaptieve snelheid wordt de dichtsbijzijnde stap direct gekozen.
        """
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
            
        gs = self.settings["global_settings"]
        speed_steps = [100, 75, 50, 25, 5, 1]
        current_direction = gs.get("ptr_speed_direction", "down")
        
        # Get the effective speed (the one that's currently being applied)
        effective_speed = self.get_effective_ptr_speed(active_apcr)
        
        # If adaptive speed is enabled, disable it and set to closest step
        if gs.get("adaptive_speed", False):
            gs["adaptive_speed"] = False
            
            # Find the closest step to the effective speed
            closest_step = min(speed_steps, key=lambda x: abs(x - effective_speed))
            
            # Set the speed directly to the closest step
            gs["ptr_speed"] = closest_step
            
            # Update the direction for next use
            current_idx = speed_steps.index(closest_step)
            if current_idx == 0:  # At max speed, direction should be down
                gs["ptr_speed_direction"] = "down"
            elif current_idx == len(speed_steps) - 1:  # At min speed, direction should be up
                gs["ptr_speed_direction"] = "up"
                
            logging.info(f"Adaptive speed disabled by PTR shortcut. Speed set directly to {closest_step}% (from adaptive {effective_speed}%)")
        else:
            # Normal elastic shortcut behavior (when adaptive speed is off)
            current_speed = gs.get("ptr_speed", 100)
            if current_speed not in speed_steps:
                current_speed = min(speed_steps, key=lambda x: abs(x - current_speed))
            
            idx = speed_steps.index(current_speed)
            
            if current_direction == "down":
                if idx < len(speed_steps) - 1:
                    next_speed = speed_steps[idx + 1]
                else:
                    next_speed = speed_steps[idx - 1]
                    gs["ptr_speed_direction"] = "up"
            else:
                if idx > 0:
                    next_speed = speed_steps[idx - 1]
                else:
                    next_speed = speed_steps[idx + 1]
                    gs["ptr_speed_direction"] = "down"
            
            gs["ptr_speed"] = next_speed
        
        # Save settings
        if MiddlethingsHandler.save_settings_func:
            MiddlethingsHandler.save_settings_func(self.settings)
        
        trigger_status_update()
        print(f"[DEBUG] PTR speed set to {gs['ptr_speed']} and direction to {gs.get('ptr_speed_direction')}")
        return "COMMAND: ACK"

    def handle_zs_shortcut(self):
        """
        Elastische shortcut voor zoom_speed:
        100 -> 75 -> 50 -> 25 -> 5 -> 1 en dan weer omhoog.
        """
        active_apcr = MiddlethingsHandler.get_active_apcr_func(MiddlethingsHandler.settings)
        if not active_apcr:
            return "ERROR: NO_ACTIVE_CAMERA"
            
        gs = self.settings["global_settings"]
        
        # Verwijderd: code om adaptive speed uit te schakelen
        
        # Originele shortcut logica behouden
        current_speed = gs.get("zoom_speed", 100)
        speed_steps = [100, 75, 50, 25, 5, 1]
        current_direction = gs.get("zoom_speed_direction", "down")
        
        if current_speed not in speed_steps:
            current_speed = min(speed_steps, key=lambda x: abs(x - current_speed))
        
        if current_direction == "down":
            idx = speed_steps.index(current_speed)
            if idx < len(speed_steps) - 1:
                next_speed = speed_steps[idx + 1]
            else:
                next_speed = speed_steps[idx - 1]
                gs["zoom_speed_direction"] = "up"
        else:
            idx = speed_steps.index(current_speed)
            if idx > 0:
                next_speed = speed_steps[idx - 1]
            else:
                next_speed = speed_steps[idx + 1]
                gs["zoom_speed_direction"] = "down"
        
        gs["zoom_speed"] = next_speed
        
        # Save settings
        if MiddlethingsHandler.save_settings_func:
            MiddlethingsHandler.save_settings_func(self.settings)
        
        trigger_status_update()
        print(f"[DEBUG] Zoom speed set to {next_speed} and direction to {gs.get('zoom_speed_direction')}")
        return "COMMAND: ACK"

    def handle_camera_selection(self, cam_number_str):
        """
        Process a command in the form 'CAM[number]' and set the selected camID in the settings.
        If the given camID doesn't exist in the configured APC-Rs, keep the previous value.
        The result is also printed to the console.
        """
        try:
            new_cam_id = int(cam_number_str)
            old_cam_id = self.settings["global_settings"].get("selected_camid")
            
            # Find the APC-R with the matching camID
            target_apcr = None
            for apcr in self.settings.get("apcrs", []):
                if apcr.get("camid") == new_cam_id:
                    target_apcr = apcr
                    break
            
            if target_apcr:
                # Set command in progress flag om status updates te vertragen
                self._command_in_progress.set()
                
                # Update de camera selectie
                self.settings["global_settings"]["selected_camid"] = new_cam_id
                
                # Save settings asynchroon
                def save_and_update():
                    try:
                        if MiddlethingsHandler.save_settings_func:
                            MiddlethingsHandler.save_settings_func(self.settings)
                        
                        # Wacht even voordat we status update triggeren
                        time.sleep(0.1)
                        
                        # Reset position cache voor de nieuwe camera
                        if hasattr(self, '_last_known_positions'):
                            self._last_known_positions.clear()
                        
                        # Trigger status update
                        trigger_status_update()
                        
                        # Reset de command flag na korte tijd
                        time.sleep(0.2)
                        self._command_in_progress.clear()
                        
                    except Exception as e:
                        logging.error(f"Error in save_and_update: {e}")
                        self._command_in_progress.clear()
                
                # Start save en update in aparte thread
                threading.Thread(target=save_and_update, daemon=True).start()
                
                msg = f"SELECT_CAM: ACK (set to {new_cam_id}, {target_apcr['name']} at {target_apcr['ip']})"
                print(msg)
                return msg
            else:
                # If the new camID is not found, keep the old value
                trigger_status_update()
                msg = f"ERROR: CAMERA {new_cam_id} NOT FOUND. Keeping {old_cam_id}"
                print(msg)
                return msg
        except Exception as e:
            logging.error(f"Camera selection error: {e}")
            msg = "ERROR: INVALID_CAMERA_ID"
            print(msg)
            return msg

    def reset_position_cache_for_camera(self, camid=None):
        """Reset de positie cache voor een specifieke camera of alle camera's."""
        if camid:
            if hasattr(self, '_last_known_positions') and camid in self._last_known_positions:
                del self._last_known_positions[camid]
            if '_global_position_cache' in globals() and camid in _global_position_cache:
                del _global_position_cache[camid]
        else:
            # Reset alle caches
            if hasattr(self, '_last_known_positions'):
                self._last_known_positions.clear()
            if '_global_position_cache' in globals():
                _global_position_cache.clear()

class ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True

def init_tcp_server(settings, send_apcr_command=None, get_active_apcr=None, save_settings_func=None):
    try:
        if not settings.get("enable_tcp_connection", False):
            print("TCP server is disabled in settings")
            return None, None
        tcp_port = settings.get("tcp_port", 11580)
        tcp_ip = settings.get("tcp_listener_ip", settings.get("listener_ip", "0.0.0.0"))
        # Stel de class attributen in
        MiddlethingsHandler.settings = settings
        MiddlethingsHandler.send_apcr_command_func = send_apcr_command
        MiddlethingsHandler.get_active_apcr_func = get_active_apcr
        MiddlethingsHandler.save_settings_func = save_settings_func
        # Wikkel de globale settings om in een ObservableSettings indien nog niet gebeurd
        if not isinstance(settings.get("global_settings", {}), ObservableSettings):
            settings["global_settings"] = ObservableSettings(
                settings.get("global_settings", {}), on_change=trigger_status_update
            )
        if tcp_ip and tcp_ip != "0.0.0.0":
            try:
                socket.inet_aton(tcp_ip)
                server_ip = tcp_ip
                print(f"TCP server will bind to specific IP: {server_ip}")
            except socket.error:
                server_ip = "0.0.0.0"
                print(f"Invalid TCP listener IP: {tcp_ip}, falling back to all interfaces")
        else:
            server_ip = "0.0.0.0"
            print("TCP server will listen on all interfaces")
        server = ThreadedTCPServer((server_ip, tcp_port), MiddlethingsHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        print(f"TCP server for Bitfocus Companion started on {server_ip}:{tcp_port}")
        debug_mode = settings.get("debug_mode", False)
        if debug_mode:
            try:
                hostname = socket.gethostname()
                ips = socket.gethostbyname_ex(hostname)[2]
                external_ips = [ip for ip in ips if not ip.startswith("127.")]
                if external_ips:
                    print("[DEBUG] Available IP addresses for Companion connection:")
                    for ip in external_ips:
                        print(f"  - {ip}:{tcp_port}")
                else:
                    print("[DEBUG] No external IP addresses found.")
            except Exception as e:
                print(f"[DEBUG] Error retrieving network information: {e}")
        return server, server_thread
    except Exception as e:
        print(f"Error starting TCP server: {e}")
        import traceback
        print(traceback.format_exc())
        return None, None

def stop_tcp_server(server, thread):
    if server:
        try:
            server.shutdown()
            server.server_close()
            if thread and thread.is_alive():
                thread.join(timeout=2.0)
            print("TCP server stopped")
        except Exception as e:
            print(f"Error stopping TCP server: {e}")



