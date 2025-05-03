# main.py
import json
import os
import pygame
import msvcrt
import socket
import threading
import queue
import time
import controls
import presets  
import logging
import interpreter 

SETTINGS_FILE = "settings.json"
PRESETS_FILE = "presets.json"
DEFAULT_PORT = 11582


MAPPING_ACTIONS = {
    "1": "map_pan_left",
    "2": "map_pan_right",
    "3": "map_tilt_up",
    "4": "map_tilt_down",
    "5": "map_roll_left",
    "6": "map_roll_right",
    "7": "map_zoom_in",
    "8": "map_zoom_out",
    "9": "map_recenter",
    "10": "map_active_track",
    "11": "map_pan_tilt_speed_increase",
    "12": "map_pan_tilt_speed_decrease",
    "13": "map_zoom_speed_increase",
    "14": "map_zoom_speed_decrease",
    "15": "map_map_pan_tilt_speed_shortcut",
    "16": "map_zoom_speed_shortcut",
    "17": "map_adaptive_speed_toggle"  # Nieuw toegevoegde mapping
}
# "18" is reserved for "return"


global_socket = None  # Globale socket voor communicatie
debug_mode = False    # Globale variabele voor debug mode
_button_preset_mapping = {}
_last_presets_mtime = None
command_queue = queue.Queue()
current_mode = "main"  
INIT_GAP           = 2.0   # seconden pauze tussen initialisaties
_last_init_started = 0.0   # tijdstip van laatste initialize_apcr_if_new


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("apcr_controller.log"),
        logging.StreamHandler()
    ]
)

_initialized_apcrs = set()

def process_command(cmd):
    """Voeg het commando toe aan de globale command_queue zodat event_loop het kan verwerken."""
    command_queue.put(cmd)

def initialize_tcp_server(settings):
    """Initialize the TCP server if enabled in settings"""
    if not settings.get("enable_tcp_connection", False):
        print("TCP server is disabled in settings")
        return None, None
        
    try:
        tcp_server, tcp_thread = interpreter.init_tcp_server(settings)
        
        if tcp_server:
            print("\n==== TCP SERVER SUCCESSFULLY STARTED ====")
            print(f"Port: {settings.get('tcp_port', 11580)}")
            print("Use one of the IP addresses listed above for Companion connection")
            print("=========================================\n")
        
        return tcp_server, tcp_thread
        
    except ImportError:
        print("ERROR: Could not import interpreter module")
        return None, None
    except Exception as e:
        print(f"ERROR starting TCP server: {e}")
        import traceback
        traceback.print_exc()
        return None, None


def initialize_apcr_if_new(apcr):
    """
    Initialize an APC-R if it hasn't been initialized before.
    Sends recenter command and idle commands for pan, tilt, and roll.
    """
    global _initialized_apcrs
    
    # Verify apcr has required fields
    if not apcr or 'camid' not in apcr or 'name' not in apcr or 'ip' not in apcr:
        print("[ERROR] initialize_apcr_if_new called with invalid apcr")
        return
    
    if apcr['camid'] in _initialized_apcrs:
        if debug_mode:
            print(f"[DEBUG] APC-R with CamID {apcr['camid']} already initialized")
        return  # Already initialized
    
    # Mark as initialized
    _initialized_apcrs.add(apcr['camid'])
    
    try:
        print(f"[{apcr['name']}] (CamID {apcr['camid']}) connected. Sending recenter command...")
        
        # Direct recenter command instead of using controls.send_recenter
        camid = int(apcr['camid'])
        recenter_data = bytes.fromhex("08" + f"{camid:02x}" + "0400000e0c0000")
        print("[DEBUG] Recenter data to send:", recenter_data.hex())
        send_apcr_command(apcr, recenter_data)
        print("Recenter command sent.")
        
        # Wait a bit for the recenter to process
        time.sleep(0.5)
        
        # Send idle commands for pan, tilt, roll and zoom
        for axis in ['pan', 'tilt', 'roll', 'zoom']:
            idle_packet = controls.get_idle_packet(axis, apcr['camid'])
            if idle_packet:
                data = bytes.fromhex(idle_packet)
                send_apcr_command(apcr, data)
                if debug_mode:
                    print(f"[DEBUG] Sent idle packet for {axis} to {apcr['name']} (CamID {apcr['camid']})")
            time.sleep(0.1)  # Short wait between commands
        
        print(f"[{apcr['name']}] (CamID {apcr['camid']}) Initialization completed.")
    except Exception as e:
        print(f"[ERROR] Failed to initialize {apcr['name']} (CamID {apcr['camid']}): {e}")

def check_for_new_apcrs(settings):
    global _last_init_started                 # ← toevoegen
    while True:
        try:
            with controls.position_lock:
                camids_with_position = set(controls.current_position.keys())

            for apcr in settings.get("apcrs", []):
                if 'camid' not in apcr:
                    logging.warning( ... )
                    continue

                # staat er al een positie én is nog niet geïnitialiseerd?
                if apcr['camid'] in camids_with_position and \
                   apcr['camid'] not in _initialized_apcrs:

                    # ---------- NIEUW: throttle ----------
                    now = time.time()
                    if now - _last_init_started < INIT_GAP:
                        # nog even wachten; we proberen in de volgende loop
                        continue
                    _last_init_started = now
                    # ---------- EINDE throttle ----------
                    
                    initialize_apcr_if_new(apcr)

            time.sleep(1.0)          # bestaande wachttijd
        except Exception as e:
            logging.error(...)
            time.sleep(1.0)


def debug_print(*args):
    """Print messages only if debug_mode is on."""
    if debug_mode:
        print(*args)

def toggle_debug_mode(settings):
    """
    Toggle debug mode on/off and update the settings.
    """
    global debug_mode
    debug_mode = not debug_mode
    
    # Also update the debug mode in the settings for other components
    settings["debug_mode"] = debug_mode
    
    # Synchronize debug mode with controls module
    controls.set_debug_mode(debug_mode)
    print(f"Debug mode is now {'ON' if debug_mode else 'OFF'}.")
    # Add an explicit debug message to confirm it works
    if debug_mode:
        print("[DEBUG] Debug mode enabled - you should now see packet data")

def load_button_preset_mappings():
    """
    Load button-to-preset mappings from the presets file.
    Only loads if the file has been modified since last load.
    
    Returns:
        dict: Mapping of (device_id, button_index) to (camid, slot_num)
    """
    global _button_preset_mapping, _last_presets_mtime
    
    try:
        # Check if the presets file exists
        if not os.path.exists(presets.PRESETS_FILE):
            return {}
        
        # Get the modification time of the presets file
        mtime = os.path.getmtime(presets.PRESETS_FILE)
        
        # If the file hasn't been modified since last load, return the cached mapping
        if _last_presets_mtime is not None and mtime <= _last_presets_mtime and _button_preset_mapping:
            return _button_preset_mapping
        
        # Load the presets file
        with open(presets.PRESETS_FILE, 'r') as f:
            presets_data = json.load(f)
        
        # Clear the existing mapping
        mapping = {}
        
        # Build the button-to-preset mapping
        if 'presets' in presets_data:
            for preset_key, preset_info in presets_data['presets'].items():
                if 'mapped_buttons' in preset_info:
                    parts = preset_key.split('.')
                    if len(parts) == 2:
                        try:
                            camid = int(parts[0])
                            slot_num = int(parts[1])
                            
                            # Add each button mapping
                            for device_id, button_index in preset_info['mapped_buttons'].items():
                                mapping[(device_id, button_index)] = (camid, slot_num)
                        except ValueError:
                            pass
        
        # Update the cache
        _button_preset_mapping = mapping
        _last_presets_mtime = mtime
        
        if debug_mode:
            print(f"[DEBUG] Loaded {len(mapping)} button-to-preset mappings from {presets.PRESETS_FILE}")
        
        return mapping
    except Exception as e:
        logging.error(f"Error loading button-to-preset mappings: {e}")
        return {}



def load_settings():
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, 'r') as f:
                s = json.load(f)
            debug_print("[DEBUG] Loaded settings from settings.json")
        else:
            s = {
                "apcrs": [],
                "global_settings": {
                    "zoom_speed": 100,
                    "ptr_speed": 100,
                    "zoom_speed_direction": "down",
                    "ptr_speed_direction": "up",
                    "preset_transition_speed": 50,
                    "selected_camid": None,
                    "position_request_frequency": 1.2,
                    "status_request_frequency": 5.0,
                    "virtualwall": False,
                    "virtualwallpreset": False,
                    "adaptive_speed": False
                },
                "devices": {},
                "listener_ip": "0.0.0.0",
                "listener_port": DEFAULT_PORT,
                "enable_tcp_connection": False,
                "tcp_port": 11580,
                "tcp_listener_ip": "0.0.0.0"
            }
            debug_print("[DEBUG] Created default settings")
   
        # Zorg dat 'apcrs', 'global_settings' en 'devices' aanwezig zijn
        if "apcrs" not in s:
            s["apcrs"] = []
        if "global_settings" not in s:
            s["global_settings"] = {
                "zoom_speed": 100,
                "ptr_speed": 100,
                "zoom_speed_direction": "down",
                "ptr_speed_direction": "up",
                "preset_transition_speed": 100,
                "selected_camid": None,
                "position_request_frequency": 1.2,
                "status_request_frequency": 1.0,
                "virtualwall": False,
                "virtualwallpreset": False,
                "adaptive_speed": False
            }
        if "devices" not in s:
            s["devices"] = {}

        # Verwijder de virtual wall instellingen uit global_settings als ze daar nog staan
        for setting in ["virtualwallstart_pan", "virtualwallend_pan", 
                        "virtualwallstart_tilt", "virtualwallend_tilt"]:
            if setting in s["global_settings"]:
                del s["global_settings"][setting]
                debug_print(f"[DEBUG] {setting} verwijderd uit global settings")
   
        if "adaptive_speed" not in s["global_settings"]:
            s["global_settings"]["adaptive_speed"] = False

        # Zorg dat de TCP instellingen aanwezig zijn
        if "enable_tcp_connection" not in s:
            s["enable_tcp_connection"] = False
        if "tcp_port" not in s:
            s["tcp_port"] = 11580
        if "tcp_listener_ip" not in s:
            s["tcp_listener_ip"] = s.get("listener_ip", "0.0.0.0")
       
        # Zet alle device-keys om naar strings
        for dev_id, dev_map in s["devices"].items():
            new_map = {}
            for k, v in dev_map.items():
                if isinstance(k, int):
                    k = str(k)
                new_map[k] = v
            s["devices"][dev_id] = new_map
   
        # Zorg dat elke APC-R een camid heeft en forceer active_track op False
        for apcr in s["apcrs"]:
            if "camid" not in apcr:
                apcr["camid"] = None
            apcr["active_track"] = False
            
            # Zorg dat elke camera alle benodigde instellingen heeft, met default waarden
            if "virtualwallstart_pan" not in apcr:
                apcr["virtualwallstart_pan"] = None
            if "virtualwallend_pan" not in apcr:
                apcr["virtualwallend_pan"] = None
            if "virtualwallstart_tilt" not in apcr:
                apcr["virtualwallstart_tilt"] = None
            if "virtualwallend_tilt" not in apcr:
                apcr["virtualwallend_tilt"] = None

        debug_print("[DEBUG] Settings loaded successfully; active_track reset to False for all APC-Rs.")
        # Sla de gewijzigde instellingen direct op zodat de file wordt overschreven
        save_settings(s)
        return s
    except Exception as e:
        print(f"[ERROR] Failed to load settings: {e}")
        return {
            "apcrs": [],
            "global_settings": {
                "zoom_speed": 100,
                "ptr_speed": 100,
                "zoom_speed_direction": "down",
                "ptr_speed_direction": "up",
                "preset_transition_speed": 100,
                "selected_camid": None,
                "position_request_frequency": 1.2,
                "status_request_frequency": 1.0,
                "virtualwall": False,
                "virtualwallpreset": False,
                "adaptive_speed": False
            },
            "devices": {},
            "listener_ip": "0.0.0.0",
            "listener_port": DEFAULT_PORT,
            "enable_tcp_connection": False,
            "tcp_port": 11580,
            "tcp_listener_ip": "0.0.0.0"
        }
    
def wait_for_current_position(apcr, timeout=1):
    """
    Wacht tot er een huidige positie beschikbaar is (maximaal 'timeout' seconden)
    en retourneer de positie als een dictionary met keys 'pan', 'tilt', 'roll' en 'zoom'.
    De functie gebruikt controls.get_current_position() om een FDB-string op te halen
    en parsed deze vervolgens.
    """
    start = time.time()
    while time.time() - start < timeout:
        pos_str = controls.get_current_position(apcr)
        if pos_str:
            try:
                # Verwacht een string in het formaat: FDB;camid;pan;tilt;roll;zoom;
                parts = pos_str.strip().split(';')
                if len(parts) >= 6:
                    return {
                        'pan': float(parts[2]),
                        'tilt': float(parts[3]),
                        'roll': float(parts[4]),
                        'zoom': float(parts[5])
                    }
            except Exception as e:
                print("[ERROR] Failed to parse current position:", e)
        time.sleep(0.1)
    return None


def save_settings(settings):
    """
    Save settings to settings.json file.
    Ensures all data is properly formatted and serializable.
    Triggers a status update for Companion after critical settings changes.
    
    Args:
        settings: The settings dictionary to save
    
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Create a copy to avoid modifying the original during saving
        settings_copy = settings.copy() if isinstance(settings, dict) else settings
        
        # Ensure all device mappings have string keys
        if "devices" in settings_copy:
            for dev_id, dev_map in settings_copy["devices"].items():
                new_map = {}
                for k, v in dev_map.items():
                    if isinstance(k, int):
                        k = str(k)
                    new_map[k] = v
                settings_copy["devices"][dev_id] = new_map
        
        # Ensure all apcrs have required fields
        if "apcrs" in settings_copy:
            for apcr in settings_copy["apcrs"]:
                if "camid" not in apcr:
                    apcr["camid"] = None
                if "active_track" not in apcr:
                    apcr["active_track"] = False
        
        # Write the settings to the file with proper indentation
        with open(SETTINGS_FILE, 'w') as f:
            json.dump(settings_copy, f, indent=4)
            
        if debug_mode:
            print(f"[DEBUG] Settings saved to {SETTINGS_FILE}")
            
        # Trigger a direct status update for Companion when critical settings are changed
        try:
            import interpreter
            interpreter.trigger_status_update()
        except ImportError:
            pass  # Ignore if interpreter module isn't loaded
        except Exception as e:
            print(f"[WARNING] Could not trigger status update: {e}")
            
        return True
    except Exception as e:
        print(f"[ERROR] Failed to save settings: {e}")
        import traceback
        traceback.print_exc()
        return False


def init_pygame():
    pygame.init()
    pygame.joystick.init()
    debug_print("[DEBUG] Pygame initialized")


def init_global_socket(settings):
    global global_socket
    try:
        # Sluit evt. oude socket
        if global_socket:
            global_socket.close()
            global_socket = None

        # Maak een nieuwe socket die alleen wordt gebruikt voor het verzenden
        new_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        new_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        new_sock.settimeout(1)

        global_socket = new_sock
        print(f"Socket voor verzenden geïnitialiseerd")
        return True
    except Exception as e:
        print(f"[ERROR] init_global_socket failed: {e}")
        global_socket = None
        return False



def udp_send_receive(ip, port, data, timeout=5, attempts=3, bind_port=None, broadcast=False):
    """
    Enhanced version of UDP send/receive that creates a dedicated socket for each transaction.
    
    Args:
        ip: IP address to send to
        port: Port to send to
        data: Bytes to send
        timeout: Timeout in seconds
        attempts: Number of attempts
        bind_port: Optional port to bind to (if None, a random port is used)
        broadcast: True to enable broadcasting
        
    Returns:
        Tuple (response_data, response_addr) or None on timeout/error
    """
    sock = None
    try:
        # Create a new socket for this transaction
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Enable broadcast if needed
        if broadcast:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        # Bind to a specific port if needed
        if bind_port:
            sock.bind(('0.0.0.0', bind_port))
        
        sock.settimeout(timeout)
        
        for attempt in range(1, attempts+1):
            if debug_mode:
                print(f"[DEBUG] Sending to {ip}:{port} (attempt {attempt}): {data.hex()}")
            
            # Send data
            sock.sendto(data, (ip, port))
            
            # Small delay to ensure packet is sent
            time.sleep(0.1)
            
            # Wait for response
            try:
                response_data, response_addr = sock.recvfrom(1024)
                
                if debug_mode:
                    print(f"[DEBUG] Received response: {response_data.hex()} from {response_addr}")
                    try:
                        response_text = response_data.decode(errors="ignore")
                        if any(c.isalnum() for c in response_text):
                            print(f"[DEBUG] Received response (text): {response_text}")
                    except:
                        pass
                
                # Return both data and address
                return (response_data, response_addr)
                
            except socket.timeout:
                if debug_mode:
                    print(f"[DEBUG] Timeout on attempt {attempt}, retrying...")
            except Exception as e:
                print(f"[ERROR] Receive error: {e}")
                # Continue to next attempt
        
        if debug_mode:
            print("[DEBUG] No response after multiple attempts.")
        
        return None
        
    except Exception as e:
        print(f"[ERROR] UDP send/receive failed: {e}")
        return None
    finally:
        # Always close the socket
        if sock:
            try:
                sock.close()
            except:
                pass

def add_apcr_connection(settings):
    """
    Enhanced version of manually adding an APC-R connection.
    Temporarily stops the UDP listener to prevent port conflicts.
    
    - Asks for IP
    - Creates a dedicated socket for this transaction
    - Sends status request (6f6b41504352)
    - Parses camid and name
    - Sends a position request (with correct camid in 2nd byte)
    - Adds new APC-R configuration to settings
    """
    # Get current UDP listener instance and stop it temporarily
    udp_listener = controls.get_udp_listener_instance()
    if udp_listener:
        print("Temporarily stopping UDP listener...")
        udp_listener.stop()
        print("UDP listener stopped.")
    
    sock = None
    try:
        ip = input("APC-R IP: ").strip()
        if not ip:
            print("No IP given, canceling.")
            return

        # Create a socket for communication
        bind_port = settings.get("listener_port", 11582)
        print(f"Creating socket on port {bind_port}...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        
        # Bind to the port - this should now work since we stopped the listener
        try:
            sock.bind(('0.0.0.0', bind_port))
            print(f"Socket bound to port {bind_port}")
        except Exception as e:
            print(f"[ERROR] Could not bind to port {bind_port}: {e}")
            return
            
        sock.settimeout(5.0)

        # 1) Status request
        print(f"Sending status request to {ip}:2390...")
        data = bytes.fromhex("6f6b41504352")
        sock.sendto(data, (ip, 2390))
        
        try:
            resp, addr = sock.recvfrom(1024)
            print(f"Received response from {addr[0]}:{addr[1]}")
        except socket.timeout:
            print(f"No response from {ip} after 5 seconds. Is the IP correct?")
            return
        except Exception as e:
            print(f"[ERROR] Failed to receive response: {e}")
            return
            
        resp_str = resp.decode(errors="ignore")
        print(f"Response: {resp_str}")
        
        parts = resp_str.split('|')
        # Example: "APCR-8399|APCR-8399|ETH|192.168.0.211|1.5.20|0|10|1"
        if len(parts) < 8:
            print(f"Invalid response format: {resp_str}")
            return

        apcr_name = parts[0].strip()
        try:
            apcr_camid = int(parts[-2])  # CamID is second-to-last field
        except ValueError:
            print(f"Could not parse CamID from response: {parts[-2]}")
            return

        print(f"Found APC-R: {apcr_name} with CamID {apcr_camid}")

        # 2) Position request
        print(f"Sending position request to {ip}:2390...")
        position_cmd_hex = "08" + f"{apcr_camid:02x}" + "0400000e140000"
        sock.sendto(bytes.fromhex(position_cmd_hex), (ip, 2390))
        
        try:
            resp2, addr2 = sock.recvfrom(1024)
            print(f"Received position data from {addr2[0]}:{addr2[1]}")
        except socket.timeout:
            print(f"No position data received from {ip} after 5 seconds.")
            return
        except Exception as e:
            print(f"[ERROR] Failed to receive position data: {e}")
            return
            
        resp2_str = resp2.decode(errors="ignore")
        print(f"Position data: {resp2_str}")
        
        if not resp2_str.startswith("FDB;"):
            print(f"Invalid position data format: {resp2_str}")
            return
            
        # Check if the camid in the FDB response matches the expected camid
        try:
            fdb_parts = resp2_str.strip().split(';')
            fdb_camid = int(fdb_parts[1])
            if fdb_camid != apcr_camid:
                print(f"[WARNING] CamID in FDB response ({fdb_camid}) doesn't match the status response ({apcr_camid}).")
                print("Using the camid from status response.")
        except Exception as e:
            print(f"[WARNING] Error checking FDB camid: {e}")

        # 3) Check if this camid is already in use by another APC-R
        for existing_apcr in settings.get("apcrs", []):
            if existing_apcr.get("camid") == apcr_camid and existing_apcr.get("ip") != ip:
                print(f"[WARNING] CamID {apcr_camid} is already in use by {existing_apcr['name']} ({existing_apcr['ip']}).")
                new_camid = input(f"Enter a different CamID for this APC-R (or press Enter to use {apcr_camid} anyway): ").strip()
                if new_camid and new_camid.isdigit():
                    apcr_camid = int(new_camid)
                    print(f"Using custom CamID: {apcr_camid}")
                break

        # 4) Add to settings
        if "apcrs" not in settings:
            settings["apcrs"] = []

        # Check if this APC-R already exists, either by name or IP
        existing_by_name = None
        existing_by_ip = None
        
        for i, apcr in enumerate(settings.get("apcrs", [])):
            if apcr.get("name") == apcr_name:
                existing_by_name = i
            if apcr.get("ip") == ip:
                existing_by_ip = i

        new_apcr_entry = {
            "name": apcr_name,
            "ip": ip,
            "camid": apcr_camid,
            "active_track": False
        }

        if existing_by_name is not None:
            # Update existing entry by name
            settings["apcrs"][existing_by_name] = new_apcr_entry
            print(f"Updated existing APC-R '{apcr_name}' with camid {apcr_camid} at IP {ip}.")
        elif existing_by_ip is not None:
            # Update existing entry by IP
            old_name = settings["apcrs"][existing_by_ip].get("name")
            settings["apcrs"][existing_by_ip] = new_apcr_entry
            print(f"Updated APC-R at {ip}: Name {old_name} -> {apcr_name}, CamID {apcr_camid}.")
        else:
            # Add new entry
            settings["apcrs"].append(new_apcr_entry)
            print(f"APC-R '{apcr_name}' added with camid {apcr_camid}.")

        save_settings(settings)

        # 5) Auto-select if it's the only APC-R
        if len(settings["apcrs"]) == 1:
            settings["global_settings"]["selected_camid"] = apcr_camid
            save_settings(settings)
            print(f"Auto-selected camid {apcr_camid} as the only available APC-R.")

    except Exception as e:
        print(f"[ERROR] Failed to add APC-R connection: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # Close the socket
        if sock:
            try:
                sock.close()
                print("Socket closed")
            except:
                pass
                
        # Restart the UDP listener
        if udp_listener:
            print("Restarting UDP listener...")
            controls.init_udp_listener(settings)
            print("UDP listener restarted.")

def discover_apcrs(settings):
    """
    Automatically discover and add APC-R devices on the network.
    Temporarily stops the UDP listener to prevent port conflicts.
    
    Returns:
        list: List of newly discovered APC-R devices
    """
    discovered = []
    sock = None
    
    # Get current UDP listener instance
    udp_listener = controls.get_udp_listener_instance()
    
    # Temporarily stop the UDP listener
    if udp_listener:
        print("Temporarily stopping UDP listener for discovery...")
        udp_listener.stop()
        print("UDP listener stopped.")
    
    try:
        # Determine broadcast address based on listener_ip
        listener_ip = settings.get("listener_ip", "0.0.0.0")
        if listener_ip == "0.0.0.0":
            # If listening on all interfaces, try to find a suitable interface
            broadcast_ips = []
            try:
                # Get all network interfaces
                interfaces = socket.getaddrinfo(socket.gethostname(), None)
                for interface in interfaces:
                    if interface[0] == socket.AF_INET:  # IPv4 only
                        ip = interface[4][0]
                        if not ip.startswith('127.'):  # Skip loopback
                            # Convert to broadcast
                            parts = ip.split('.')
                            if len(parts) == 4:
                                broadcast_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                                broadcast_ips.append(broadcast_ip)
            except:
                pass
                
            # If we couldn't find suitable interfaces, use the default broadcast
            if not broadcast_ips:
                broadcast_ips = ["255.255.255.255"]
                print("Broadcasting to all network interfaces (255.255.255.255)")
            else:
                print(f"Broadcasting to detected subnets: {', '.join(broadcast_ips)}")
        else:
            # Calculate broadcast address for specific subnet
            parts = listener_ip.split('.')
            if len(parts) != 4:
                # Invalid IP format, use default broadcast
                broadcast_ips = ["255.255.255.255"]
                print("Using default broadcast address (255.255.255.255)")
            else:
                # Calculate broadcast address (last octet to 255)
                broadcast_ip = f"{parts[0]}.{parts[1]}.{parts[2]}.255"
                broadcast_ips = [broadcast_ip]
                print(f"Broadcasting to subnet {broadcast_ip}")
        
        # APC-R discovery packet (status request)
        discovery_packet = bytes.fromhex("6f6b41504352")
        
        # Create a socket for broadcasting and receiving on the same port as usual
        bind_port = settings.get("listener_port", 11582)
        
        print(f"Creating socket for discovery on port {bind_port}...")
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        
        # Bind to the port - this should now work since we stopped the listener
        try:
            sock.bind(('0.0.0.0', bind_port))
            print(f"Socket bound to port {bind_port}")
        except Exception as e:
            print(f"[WARNING] Could not bind to port {bind_port}: {e}")
            return []  # Return early if binding fails
        
        # Set a timeout for receiving
        sock.settimeout(5.0)
        
        # Broadcast to all determined broadcast addresses
        responses = []
        for broadcast_ip in broadcast_ips:
            print(f"Sending broadcast discovery packet to {broadcast_ip}:2390...")
            
            # Send the discovery packet
            sock.sendto(discovery_packet, (broadcast_ip, 2390))
            
            # Wait for responses with timeout
            start_time = time.time()
            while time.time() - start_time < 3.0:  # Listen for responses for 3 seconds
                try:
                    data, addr = sock.recvfrom(1024)
                    if data:
                        print(f"Received response from {addr[0]}:{addr[1]} - {len(data)} bytes")
                        try:
                            resp_str = data.decode(errors="ignore")
                            print(f"Response: {resp_str}")
                            responses.append((data, addr))
                        except:
                            print(f"Could not decode response: {data.hex()}")
                except socket.timeout:
                    # No more responses
                    break
                except Exception as e:
                    print(f"[ERROR] Error receiving response: {e}")
                    break
        
        if not responses:
            print("No APC-R devices responded to discovery.")
            return []
            
        # Process all unique responses
        processed_ips = set()
        for resp, addr in responses:
            ip = addr[0]
            
            # Skip duplicate responses
            if ip in processed_ips:
                continue
                
            processed_ips.add(ip)
            
            try:
                resp_str = resp.decode(errors="ignore")
                parts = resp_str.split('|')
                
                if len(parts) < 8:
                    print(f"Invalid response from {ip}: {resp_str}")
                    continue
                    
                apcr_name = parts[0].strip()
                try:
                    apcr_camid = int(parts[-2])  # CamID is second-to-last field
                except ValueError:
                    print(f"Could not parse CamID from {parts[-2]}")
                    continue
                
                print(f"Found APC-R at {ip}: {apcr_name} with CamID {apcr_camid}")
                
                # Check if this APC-R is already in our settings (by name)
                existing_by_name = None
                existing_by_ip = None
                
                for i, apcr in enumerate(settings.get("apcrs", [])):
                    if apcr.get("name") == apcr_name:
                        existing_by_name = i
                    if apcr.get("ip") == ip:
                        existing_by_ip = i
                
                # If we have a match by name, potentially update IP and camID
                if existing_by_name is not None:
                    old_ip = settings["apcrs"][existing_by_name].get("ip")
                    old_camid = settings["apcrs"][existing_by_name].get("camid")
                    
                    # Only update if something changed
                    if old_ip != ip or old_camid != apcr_camid:
                        settings["apcrs"][existing_by_name]["ip"] = ip
                        settings["apcrs"][existing_by_name]["camid"] = apcr_camid
                        print(f"Updated existing APC-R '{apcr_name}': IP {old_ip} -> {ip}, CamID {old_camid} -> {apcr_camid}")
                        discovered.append(apcr_name)
                        
                # Else if we have a match by IP but different name, the device may have been renamed
                elif existing_by_ip is not None:
                    old_name = settings["apcrs"][existing_by_ip].get("name")
                    old_camid = settings["apcrs"][existing_by_ip].get("camid")
                    
                    # Only update if something changed
                    if old_name != apcr_name or old_camid != apcr_camid:
                        settings["apcrs"][existing_by_ip]["name"] = apcr_name
                        settings["apcrs"][existing_by_ip]["camid"] = apcr_camid
                        print(f"Updated APC-R at {ip}: Name {old_name} -> {apcr_name}, CamID {old_camid} -> {apcr_camid}")
                        discovered.append(apcr_name)
                        
                # Otherwise it's a new device
                else:
                    new_apcr_entry = {
                        "name": apcr_name,
                        "ip": ip,
                        "camid": apcr_camid,
                        "active_track": False
                    }
                    
                    # Check for conflicting camid
                    has_conflict = False
                    for apcr in settings.get("apcrs", []):
                        if apcr.get("camid") == apcr_camid:
                            print(f"[WARNING] CamID {apcr_camid} is already in use by {apcr['name']} ({apcr['ip']}).")
                            has_conflict = True
                            break
                    
                    # Add to settings
                    if "apcrs" not in settings:
                        settings["apcrs"] = []
                        
                    settings["apcrs"].append(new_apcr_entry)
                    print(f"Added new APC-R '{apcr_name}' at {ip} with CamID {apcr_camid}" + 
                          (" (has CamID conflict)" if has_conflict else ""))
                    discovered.append(apcr_name)
                    
            except Exception as e:
                print(f"[ERROR] Failed to process response from {ip}: {e}")
                import traceback
                traceback.print_exc()
        
        # Save settings with all updates
        if discovered:
            save_settings(settings)
            
            # Auto-select if there's only one APC-R
            if len(settings["apcrs"]) == 1:
                settings["global_settings"]["selected_camid"] = settings["apcrs"][0]["camid"]
                save_settings(settings)
                print(f"Auto-selected camid {settings['apcrs'][0]['camid']} as the only available APC-R.")
                
        return discovered
        
    except Exception as e:
        print(f"[ERROR] Failed to discover APC-Rs: {e}")
        import traceback
        traceback.print_exc()
        return []
    finally:
        # Always close the socket
        if sock:
            try:
                sock.close()
                print("Discovery socket closed")
            except:
                pass
        
        # Restart the UDP listener
        if udp_listener:
            print("Restarting UDP listener...")
            controls.init_udp_listener(settings)
            print("UDP listener restarted.")


def remove_apcr_connection(settings):
    try:
        apcrs = settings.get("apcrs", [])
        if not apcrs:
            print("No APC-R connections to remove.")
            return
        print("Select the APC-R to remove or type 'cancel':")
        for i, a in enumerate(apcrs, start=1):
            print(f"{i}. {a['name']} ({a['ip']})")
        while True:
            choice = input("> ").strip().lower()
            if choice == "cancel":
                return
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(apcrs):
                    removed = apcrs.pop(idx - 1)
                    save_settings(settings)
                    print(f"Removed APC-R: {removed['name']}")
                    # Update selected_camid indien nodig
                    if settings["global_settings"].get("selected_camid") == removed["camid"]:
                        if apcrs:
                            settings["global_settings"]["selected_camid"] = apcrs[0]["camid"]
                            print(f"Auto-selected camid {apcrs[0]['camid']} as the new selected_camid.")
                        else:
                            settings["global_settings"]["selected_camid"] = None
                            print("No APC-Rs left. selected_camid set to None.")
                        save_settings(settings)
                    return
            print("Invalid choice, try again.")
    except Exception as e:
        print(f"[ERROR] Failed to remove APC-R connection: {e}")


def set_listener_ip(settings):
    try:
        val = input("IP and port local interface Example '192.168.0.1:11582' or just '192.168.0.1': ").strip()
        if not val:
            print("No input, canceling.")
            return

        if ':' in val:
            ip, port_str = val.split(':', 1)
            ip = ip.strip()
            port_str = port_str.strip()
            if ip == '':
                ip = None
            if port_str == '':
                port = DEFAULT_PORT
            else:
                try:
                    port = int(port_str)
                except ValueError:
                    print("Invalid port. Using default port.")
                    port = DEFAULT_PORT
        else:
            ip = val.strip()
            if ip == '':
                ip = None
            port = DEFAULT_PORT

        if ip is not None:
            try:
                socket.inet_aton(ip)  # valid format check
            except OSError:
                print("Invalid IP address format. Settings not updated.")
                return

        settings["listener_ip"] = ip
        settings["listener_port"] = port
        save_settings(settings)
        print(f"Listener updated to IP: {ip or '0.0.0.0'} Port: {port}")
        
        # Na veranderen van IP/poort opnieuw global_socket initialiseren.
        init_global_socket(settings)
        
        # Ook de UDP listener opnieuw initialiseren
        udp_listener = controls.get_udp_listener_instance()
        if udp_listener:
            print("Restarting UDP listener with new settings...")
            udp_listener.stop()
        if controls.init_udp_listener(settings):
            print(f"UDP listener restarted on {ip or '0.0.0.0'}:{port}")
        else:
            print("[ERROR] Failed to restart UDP listener with new settings")
        
    except Exception as e:
        print(f"[ERROR] Failed to set listener IP: {e}")


def send_apcr_command(apcr, data):
    """
    Send data via the global socket to apcr['ip']:2390.
    Port 2390 is always used for sending to APC-R devices.
    
    Args:
        apcr: Dictionary containing at least 'ip' and 'camid' keys
        data: Binary data to send, usually in hex format
    """
    global global_socket

    # Verify that we have a valid apcr with required fields
    if not apcr:
        print("[ERROR] send_apcr_command called with None apcr")
        return
        
    if 'ip' not in apcr:
        print("[ERROR] send_apcr_command called with apcr missing 'ip' field")
        return
        
    if 'camid' not in apcr:
        print("[ERROR] send_apcr_command called with apcr missing 'camid' field")
        return

    # Extra safety check for global_socket
    if global_socket is None:
        print("[WARN] global_socket is None. Trying to re-init socket.")
        init_global_socket(load_settings())

    try:
        if global_socket is None:
            # Failed to re-init: show error, give up
            print("[ERROR] Could not re-init global socket. Not sending.")
            return

        ip = apcr['ip']
        port = 2390  # Always use port 2390 for sending
        camid = apcr.get('camid', 'unknown')
        
        # Improved debug output
        if debug_mode:
            print(f"[DEBUG] Sending packet to {ip}:{port} (CamID {camid}): {data.hex()}")

        global_socket.sendto(data, (ip, port))
    except Exception as e:
        print(f"[ERROR] Failed to send APC-R command: {e}")

def get_active_apcr(settings):
    """
    Get the active APC-R based on the selected_camid in global_settings.
    If no camid is selected but there's only one APC-R, that one is auto-selected.
    
    Args:
        settings: Application settings dictionary
        
    Returns:
        dict: The active APC-R configuration or None if none is selected/available
    """
    try:
        # Get the selected camera ID from settings
        selected_camid = settings["global_settings"].get("selected_camid")
        
        # Debug output
        if debug_mode:
            print(f"[DEBUG] get_active_apcr called, selected_camid = {selected_camid}")
        
        # If no camera is selected, check if we can auto-select one
        if selected_camid is None:
            apcrs = settings.get("apcrs", [])
            
            # If there's only one APC-R, automatically select it
            if len(apcrs) == 1:
                selected_camid = apcrs[0]["camid"]
                settings["global_settings"]["selected_camid"] = selected_camid
                save_settings(settings)
                print(f"Auto-selected camid {selected_camid} as the only available APC-R.")
            elif len(apcrs) > 1:
                print("Multiple APC-Rs configured, but no camid selected. Please select one using the 'cam[num]' command.")
                return None
            else:
                # No APC-Rs configured
                if debug_mode:
                    print("[DEBUG] No APC-Rs configured in settings.")
                return None
        
        # Find the APC-R that matches the selected camid
        for apcr in settings.get("apcrs", []):
            if apcr.get("camid") == selected_camid:
                if debug_mode:
                    print(f"[DEBUG] Found active APC-R: {apcr['name']} (CamID={selected_camid}, IP={apcr['ip']})")
                return apcr
        
        # If we get here, the selected camid doesn't match any APC-R
        print(f"[WARNING] Selected camid {selected_camid} not found among configured APC-Rs.")
        
        # If there are APC-Rs configured, suggest switching to one of them
        apcrs = settings.get("apcrs", [])
        if apcrs:
            apcr_list = ", ".join([f"{a['camid']} ({a['name']})" for a in apcrs])
            print(f"Available APC-Rs: {apcr_list}")
            print("Use 'cam[num]' to select one of these cameras.")
        
        return None
    except Exception as e:
        print(f"[ERROR] Failed to get active APC-R: {e}")
        return None
    
def save_preset_with_cache_invalidation(camid, slot_num, position, mapped_button=None, device_id=None):
    """
    Wrapper for presets.save_preset that also invalidates the button mapping cache.
    """
    result = presets.save_preset(camid, slot_num, position, mapped_button, device_id)
    if result:
        invalidate_button_mapping_cache()
    return result

def check_button_mapped_to_preset(device_id, button_index):
    """
    Check if a button is mapped to a preset using the cached mapping.
    
    Args:
        device_id: Device ID (guid or name)
        button_index: Button index
        
    Returns:
        tuple: (camid, slot_num) if mapped, or None if not mapped
    """
    mapping = load_button_preset_mappings()
    return mapping.get((device_id, button_index))



def delete_preset_with_cache_invalidation(camid, slot_num):
    """
    Wrapper for presets.delete_preset that also invalidates the button mapping cache.
    """
    result = presets.delete_preset(camid, slot_num)
    if result:
        invalidate_button_mapping_cache()
    return result


def delete_all_presets_with_cache_invalidation():
    """
    Wrapper for presets.delete_all_presets that also invalidates the button mapping cache.
    """
    result = presets.delete_all_presets()
    if result:
        invalidate_button_mapping_cache()
    return result

def invalidate_button_mapping_cache():
    """
    Invalidate the button-to-preset mapping cache.
    Call this function whenever presets are created, modified, or deleted.
    """
    global _last_presets_mtime
    _last_presets_mtime = None
    if debug_mode:
        print("[DEBUG] Button-to-preset mapping cache invalidated")

def event_loop(settings):
    """
    Event loop that processes both joystick and console input.
    Console input is read via a separate thread with input("> "),
    so that standard terminal editing (backspace, cursor movements, etc.)
    is available.
    """
    global debug_mode
    import time, queue


    stop_event = threading.Event()  # to stop the input thread

    def read_input():
        while not stop_event.is_set():
            try:
                cmd = input("> ")
                command_queue.put(cmd)
            except (EOFError, RuntimeError):
                # sys.stdin is niet beschikbaar, dus beëindig de thread.
                break

    input_thread = threading.Thread(target=read_input, daemon=True)
    input_thread.start()


    # Check if a joystick is connected
    if pygame.joystick.get_count() == 0:
        print("No joysticks found. Connect a joystick and restart.")
        stop_event.set()
        return

    joystick = pygame.joystick.Joystick(0)
    joystick.init()
    debug_print("[DEBUG] Initialized joystick:", joystick.get_name())

    apcr = get_active_apcr(settings)
    if apcr is None:
        print("No APC-R configured or selected.")
        stop_event.set()
        return

    device_id = apcr.get("device_id", None)
    if not device_id:
        device_id = joystick.get_guid() if hasattr(joystick, 'get_guid') else joystick.get_name()

    settings["devices"][device_id] = settings["devices"].get(device_id, {})
    device_mappings = settings["devices"][device_id]
    if not isinstance(device_mappings, dict):
        print(f"[ERROR] device_mappings is not a dictionary, got: {type(device_mappings)}")
        stop_event.set()
        return

    clock = pygame.time.Clock()
    running = True
    prev_axis_values = [0.0] * joystick.get_numaxes()
    prev_button_state = {i: False for i in range(joystick.get_numbuttons())}

    while running:
        any_user_input = False
        pygame.event.pump()

        # Process joystick buttons
        for btn_idx in range(joystick.get_numbuttons()):
            pressed = joystick.get_button(btn_idx)
            if pressed and not prev_button_state[btn_idx]:      
                for key, mapping in device_mappings.items():    
                    if not key.startswith("camid_"):            
                        continue                                
                    if mapping.get("type") != "button":         
                        continue                                
                    if mapping.get("index") != btn_idx:         
                        continue                                
                    camid_num = int(key.split("_")[1])          
                    settings["global_settings"]["selected_camid"] = camid_num   
                    save_settings(settings)                     
                    print(f"Selected camid {camid_num}.")       
                    prev_button_state[btn_idx] = pressed        
                    break                                       
                else:                                           
                    pass  # geen CamID‑hit; ga verder            

            if pressed:
                any_user_input = True
            if pressed != prev_button_state[btn_idx]:
                # Check if this button is mapped to a preset (ONLY when button is pressed, not released)
                if pressed:
                    # Get the device ID for preset mapping lookup
                    joystick_id = joystick.get_guid() if hasattr(joystick, 'get_guid') else joystick.get_name()
                    
                    # Check if this button is mapped to a preset
                    preset_info = check_button_mapped_to_preset(joystick_id, btn_idx)
                    
                    if preset_info:
                        camid, slot_num = preset_info
                        # Find the APC-R associated with this camid
                        preset_apcr = None
                        for a in settings.get("apcrs", []):
                            if a.get("camid") == camid:
                                preset_apcr = a
                                break
                        
                        if preset_apcr:
                            # Recall the preset
                            if debug_mode:
                                print(f"[DEBUG] Recalling preset {camid}.{slot_num} via button {btn_idx}")
                            print(f"Recalling preset {camid}.{slot_num} via button {btn_idx}")
                            presets.recall_preset(camid, slot_num, preset_apcr, settings)
                            # Skip normal button processing after preset recall
                            prev_button_state[btn_idx] = pressed
                            continue
                
                # Normal button action processing (if not a preset button or if release event)
                action_key = find_action_for_button(device_mappings, btn_idx)
                if action_key is not None:
                    map_name = MAPPING_ACTIONS.get(action_key)
                    debug_print("[DEBUG] map_name for action_key", action_key, ":", map_name)
                    if map_name:
                        is_shortcut = map_name in [
                            "map_map_pan_tilt_speed_shortcut",
                            "map_zoom_speed_shortcut"
                        ]
                        non_continuous = map_name in [
                            "map_recenter", "map_active_track",
                            "map_pan_tilt_speed_increase", "map_pan_tilt_speed_decrease",
                            "map_zoom_speed_increase", "map_zoom_speed_decrease",
                            "map_adaptive_speed_toggle"
                        ]
                        mapping = device_mappings.get(action_key)
                        if not mapping:
                            debug_print("[DEBUG] No mapping found for action_key:", action_key)
                        else:
                            mappings_list = mapping if isinstance(mapping, list) else [mapping]
                            for m in mappings_list:
                                debug_print("[DEBUG] Processing mapping:", m)
                                if pressed:
                                    # Make sure we have the latest active APC-R before processing any joystick input
                                    apcr = get_active_apcr(settings)
                                    if not apcr:
                                        print("[ERROR] No active APC-R found. Please select a valid camera.")
                                        break
                                        
                                    if is_shortcut or non_continuous:
                                        call_non_continuous_action(map_name, apcr, settings)
                                    else:
                                        as_name, direction = map_action_to_as_and_dir(map_name, trigger_type='button')
                                        if as_name and direction:
                                            percentage = (settings["global_settings"].get('zoom_speed', 100)
                                                          if as_name == 'zoom'
                                                          else settings["global_settings"].get('ptr_speed', 100))
                                            debug_print("[DEBUG] Using percentage:", percentage,
                                                        "for as_name:", as_name)
                                            controls.start_or_update_movement(as_name, direction, percentage, send_apcr_command, apcr, control_type='button', settings=settings)
                                else:
                                    if not (is_shortcut or non_continuous):
                                        as_name, _ = map_action_to_as_and_dir(map_name, trigger_type='button')
                                        if as_name:
                                            # Always get latest apcr for stop operations too
                                            apcr = get_active_apcr(settings)
                                            if apcr:
                                                controls.stop_movement(as_name, send_apcr_command, apcr)
                prev_button_state[btn_idx] = pressed

        # Process joystick axes
        for axis_idx in range(joystick.get_numaxes()):
            value = joystick.get_axis(axis_idx)
            # --- A) **CamID‑mapping via axis** --------------------
            if abs(value) > 0.4:
                any_user_input = True                              
                for key, mapping in device_mappings.items():      
                    if not key.startswith("camid_"):               
                        continue                                   
                    if mapping.get("type") != "axis":             
                        continue                                   
                    if mapping.get("index") != axis_idx:           
                        continue                                  
                    dir_needed = mapping.get("direction")          
                    dir_now    = "positive" if value > 0 else "negative"  
                    if dir_needed != dir_now:                     
                        continue                                   
                    camid_num = int(key.split("_")[1])            
                    settings["global_settings"]["selected_camid"] = camid_num   
                    save_settings(settings)                        
                    print(f"Selected camid {camid_num}.")          
                    break                                         



            if abs(value - prev_axis_values[axis_idx]) > 0.01:
                action_key = find_action_for_axis(device_mappings, axis_idx)
                debug_print(f"[DEBUG] Axis {axis_idx} -> action_key:", action_key)
                if action_key is not None:
                    map_name = MAPPING_ACTIONS.get(action_key)
                    if map_name:
                        # Make sure we have the latest active APC-R before processing any joystick input
                        apcr = get_active_apcr(settings)
                        if not apcr:
                            print("[ERROR] No active APC-R found. Please select a valid camera.")
                            break
                            
                        non_continuous = map_name in [
                            "map_recenter", "map_active_track",
                            "map_pan_tilt_speed_increase", "map_pan_tilt_speed_decrease",
                            "map_zoom_speed_increase", "map_zoom_speed_decrease"
                        ]
                        mapping = device_mappings.get(action_key)
                        if mapping and isinstance(mapping, (list, dict)):
                            mappings_list = mapping if isinstance(mapping, list) else [mapping]
                            for m in mappings_list:
                                if non_continuous:
                                    if abs(value) > 0.5:
                                        call_non_continuous_action(map_name, apcr, settings)
                                else:
                                    if abs(value) < 0.05:
                                        as_name, _ = map_action_to_as_and_dir(map_name, trigger_type='axis')
                                        if as_name:
                                            controls.stop_movement(as_name, send_apcr_command, apcr)
                                    else:
                                        as_name, _ = map_action_to_as_and_dir(map_name, trigger_type='axis')
                                        if as_name:
                                            direction = ('out' if value > 0 else 'in') if as_name == 'zoom' else \
                                                        ('positive' if value > 0 else 'negative')
                                            percentage = (int(settings["global_settings"].get('zoom_speed', 100) * abs(value))
                                                          if as_name == 'zoom'
                                                          else int(settings["global_settings"].get('ptr_speed', 100) * abs(value)))
                                            percentage = max(1, min(percentage, 100))
                                            controls.start_or_update_movement(as_name, direction, percentage, send_apcr_command, apcr, control_type='axis', settings=settings)
                prev_axis_values[axis_idx] = value

        # Processing console commands from the command_queue
        while not command_queue.empty():
            command = command_queue.get().strip().lower()
            if command == "":
                continue


            if command.startswith("save"):
                arg = command[4:].strip()
                if '.' in arg:
                    parts = arg.split('.', 1)
                    if parts[0].isdigit() and parts[1].isdigit():
                        camid = int(parts[0])
                        slot_num = int(parts[1])
                    else:
                        print("Invalid save command format. Use save[camID].[slot], e.g. save1.1")
                        continue
                else:
                    # No camID specified: use the actively selected APC‑R
                    if not arg.isdigit():
                        print("Invalid save command format. Use save[slot], e.g. save1")
                        continue
                    slot_num = int(arg)
                    active_apcr = get_active_apcr(settings)
                    if not active_apcr:
                        print("No active APC-R found.")
                        continue
                    camid = active_apcr["camid"]
                
                # Try to get the position from current_position first
                active_apcr = get_active_apcr(settings)
                if not active_apcr:
                    print("No active APC-R found.")
                    continue
                
                curr_pos_str = controls.get_current_position(active_apcr)
                success = False
                
                if curr_pos_str:
                    # Parse the FDB string to a dictionary
                    try:
                        parts = curr_pos_str.strip().split(';')
                        curr_pos = {
                            'pan': float(parts[2]),
                            'tilt': float(parts[3]),
                            'roll': float(parts[4]),
                            'zoom': float(parts[5])
                        }
                        if debug_mode:
                            print(f"[DEBUG] Position from get_current_position: {curr_pos}")
                        
                        success = presets.save_preset(camid, slot_num, curr_pos)
                    except Exception as e:
                        print(f"[ERROR] Failed to parse current position: {e}")
                        print("Trying alternative method...")
                        # If parsing fails, try the alternative method
                        curr_pos = controls.wait_for_current_position(active_apcr, timeout=2.5)
                        if curr_pos:
                            if debug_mode:
                                print(f"[DEBUG] Position from wait_for_current_position: {curr_pos}")
                            
                            success = presets.save_preset(camid, slot_num, curr_pos)
                        else:
                            print("[ERROR] Current position data is not available after multiple attempts.")
                else:
                    # If get_current_position fails, try wait_for_current_position
                    print("[DEBUG] get_current_position gave no result, trying wait_for_current_position...")
                    curr_pos = controls.wait_for_current_position(active_apcr, timeout=2.5)
                    if curr_pos:
                        if debug_mode:
                            print(f"[DEBUG] Position from wait_for_current_position: {curr_pos}")
                        
                        success = presets.save_preset(camid, slot_num, curr_pos)
                    else:
                        print("[ERROR] Current position data is not available.")
                
                # Only show a success message if the save actually succeeded
                if success:
                    print(f"Saved current position to slot {camid}.{slot_num}.")
                else:
                    print(f"[ERROR] Failed to save position to slot {camid}.{slot_num}.")

            elif command.startswith("recall"):
                arg = command[6:].strip()
                if '.' in arg:
                    parts = arg.split('.', 1)
                    if parts[0].isdigit() and parts[1].isdigit():
                        camid = int(parts[0])
                        slot_num = int(parts[1])
                    else:
                        print("Invalid recall format. Use recall[camID].[slot], e.g. recall1.1")
                        continue
                else:
                    # No camID specified: use the actively selected APC‑R
                    if not arg.isdigit():
                        print("Invalid recall command format. Use recall[slot], e.g. recall1")
                        continue
                    slot_num = int(arg)
                    active_apcr = get_active_apcr(settings)
                    if not active_apcr:
                        print("No active APC-R found.")
                        continue
                    camid = active_apcr["camid"]
                
                # Find the APC-R that matches the requested camid
                target_apcr = None
                for apcr_item in settings.get("apcrs", []):
                    if apcr_item.get("camid") == camid:
                        target_apcr = apcr_item
                        break
                
                if not target_apcr:
                    print(f"No APC-R found with CamID {camid}.")
                    continue
                
                # Start the recall
                presets.recall_preset(camid, slot_num, target_apcr, settings)
                
            elif command == "debug":
                toggle_debug_mode(settings)

            elif command.startswith("transitionspeed"):
                try:
                    value_str = command.replace("transitionspeed", "").strip()
                    if value_str:
                        value = int(value_str)
                        if 1 <= value <= 100:
                            settings["global_settings"]["preset_transition_speed"] = value
                            save_settings(settings)
                            print(f"Preset transition speed set to {value}%")
                        else:
                            print("Value must be between 1 and 100")
                    else:
                        # Show current value if no new value provided
                        current = settings["global_settings"].get("preset_transition_speed", 100)
                        print(f"Current preset transition speed: {current}%")
                except ValueError:
                    print("Invalid value. Please use a number between 1 and 100")

            elif command == "help":
                show_help()

            elif command == "settings":
                print("Returning to main menu...")
                running = False
                break

            elif command in ["quit", "exit"]:
                print("Exiting...")
                running = False
                break

            elif command.startswith("virtualwall "):
                # Expected commands: "virtualwall on" or "virtualwall off"
                parts = command.split()
                if len(parts) >= 2:
                    if parts[1] == "on":
                        settings["global_settings"]["virtualwall"] = True
                        # Reset the virtual wall tracker so no old blockages remain
                        controls.virtual_wall_tracker["pan"] = None
                        controls.virtual_wall_tracker["tilt"] = None
                        save_settings(settings)
                        print("Virtual wall for manual control ENABLED.")
                        print("Movement will be blocked if it enters the virtual wall boundaries.")
                        
                        # Get active camera to check its virtual wall settings
                        active_apcr = get_active_apcr(settings)
                        if active_apcr:
                            # Display current wall settings if available for the active camera
                            if active_apcr.get("virtualwallstart_pan") is not None and active_apcr.get("virtualwallend_pan") is not None:
                                start_pan = active_apcr["virtualwallstart_pan"]
                                end_pan = active_apcr["virtualwallend_pan"]
                                print(f"  Pan virtual wall: {start_pan}° to {end_pan}°")
                            else:
                                print(f"  No pan virtual wall boundaries set yet for {active_apcr['name']}.")
                                
                            if active_apcr.get("virtualwallstart_tilt") is not None and active_apcr.get("virtualwallend_tilt") is not None:
                                start_tilt = active_apcr["virtualwallstart_tilt"]
                                end_tilt = active_apcr["virtualwallend_tilt"]
                                print(f"  Tilt virtual wall: {start_tilt}° to {end_tilt}°")
                            else:
                                print(f"  No tilt virtual wall boundaries set yet for {active_apcr['name']}.")
                        else:
                            print("  No camera selected. Please select a camera first.")
                            
                    elif parts[1] == "off":
                        settings["global_settings"]["virtualwall"] = False
                        # Reset tracker
                        controls.virtual_wall_tracker["pan"] = None
                        controls.virtual_wall_tracker["tilt"] = None
                        save_settings(settings)
                        print("Virtual wall for manual control DISABLED.")
                        print("Movement will NOT be blocked by virtual wall boundaries.")
                    else:
                        print("Usage: virtualwall on|off")
                else:
                    print("Usage: virtualwall on|off")

            elif command.startswith("virtualwallpreset "):
                # Expect commands: "virtualwallpreset on" or "virtualwallpreset off"
                parts = command.split()
                if len(parts) >= 2:
                    if parts[1] == "on":
                        settings["global_settings"]["virtualwallpreset"] = True
                        save_settings(settings)
                        print("Virtual wall for preset recalls enabled.")
                    elif parts[1] == "off":
                        settings["global_settings"]["virtualwallpreset"] = False
                        save_settings(settings)
                        print("Virtual wall for preset recalls disabled.")
                    else:
                        print("Usage: virtualwallpreset on|off")
                else:
                    print("Usage: virtualwallpreset on|off")

            elif command == "virtualwallpanclear":
                # Reset both start and end values for the pan virtual wall in the active camera
                active_apcr = get_active_apcr(settings)
                if not active_apcr:
                    print("No active APC-R found.")
                else:
                    active_apcr["virtualwallstart_pan"] = None
                    active_apcr["virtualwallend_pan"] = None
                    save_settings(settings)
                    print(f"Virtual wall pan boundaries cleared for {active_apcr['name']}.")
                
            elif command == "virtualwalltiltclear":
                # Reset both start and end values for the tilt virtual wall in the active camera
                active_apcr = get_active_apcr(settings)
                if not active_apcr:
                    print("No active APC-R found.")
                else:
                    active_apcr["virtualwallstart_tilt"] = None
                    active_apcr["virtualwallend_tilt"] = None
                    save_settings(settings)
                    print(f"Virtual wall tilt boundaries cleared for {active_apcr['name']}.")
                    
            elif command == "virtualwallpanstart":
                active_apcr = get_active_apcr(settings)
                if not active_apcr:
                    print("No active APC-R found.")
                else:
                    curr_pos = controls.wait_for_current_position(active_apcr, timeout=2.5)
                    if curr_pos:
                        try:
                            pan_val = float(curr_pos['pan'])
                            active_apcr["virtualwallstart_pan"] = pan_val
                            save_settings(settings)
                            print(f"Virtual wall pan start set to {pan_val}° for {active_apcr['name']}")
                        except Exception as e:
                            print("[ERROR] Could not parse pan value:", e)
                    else:
                        print("[ERROR] No current position available after waiting.")
                        
            elif command == "virtualwallpanend":
                active_apcr = get_active_apcr(settings)
                if not active_apcr:
                    print("No active APC-R found.")
                else:
                    curr_pos = controls.wait_for_current_position(active_apcr, timeout=2.5)
                    if curr_pos:
                        try:
                            pan_val = float(curr_pos['pan'])
                            active_apcr["virtualwallend_pan"] = pan_val
                            save_settings(settings)
                            print(f"Virtual wall pan end set to {pan_val}° for {active_apcr['name']}")
                        except Exception as e:
                            print("[ERROR] Could not parse pan value:", e)
                    else:
                        print("[ERROR] No current position available after waiting.")
                        
            elif command == "virtualwalltiltstart":
                active_apcr = get_active_apcr(settings)
                if not active_apcr:
                    print("No active APC-R found.")
                else:
                    curr_pos = controls.wait_for_current_position(active_apcr, timeout=2.5)
                    if curr_pos:
                        try:
                            tilt_val = float(curr_pos['tilt'])
                            active_apcr["virtualwallstart_tilt"] = tilt_val
                            save_settings(settings)
                            print(f"Virtual wall tilt start set to {tilt_val}° for {active_apcr['name']}")
                        except Exception as e:
                            print("[ERROR] Could not parse tilt value:", e)
                    else:
                        print("[ERROR] No current position available.")
                        
            elif command == "virtualwalltiltend":
                active_apcr = get_active_apcr(settings)
                if not active_apcr:
                    print("No active APC-R found.")
                else:
                    curr_pos = controls.wait_for_current_position(active_apcr, timeout=2.5)
                    if curr_pos:
                        try:
                            tilt_val = float(curr_pos['tilt'])
                            active_apcr["virtualwallend_tilt"] = tilt_val
                            save_settings(settings)
                            print(f"Virtual wall tilt end set to {tilt_val}° for {active_apcr['name']}")
                        except Exception as e:
                            print("[ERROR] Could not parse tilt value:", e)
                    else:
                        print("[ERROR] No current position available.")

            elif command.startswith("adaptivespeed "):
                # Extract the parameter (on/off)
                param = command.replace("adaptivespeed ", "").strip().lower()
                if param == "on":
                    settings["global_settings"]["adaptive_speed"] = True
                    save_settings(settings)
                    print("Adaptive speed is now ON")
                    print("PTR speed will automatically adjust based on zoom level")
                elif param == "off":
                    settings["global_settings"]["adaptive_speed"] = False
                    save_settings(settings)
                    print("Adaptive speed is now OFF")
                    print("PTR speed will use the fixed value from settings")
                else:
                    print("Usage: adaptivespeed on|off")

            elif command.startswith("cam"):
                camid_str = command[3:]
                if camid_str.isdigit():
                    camid = int(camid_str)
                    
                    # Verify if the requested camid exists in the configured APC-Rs
                    cam_exists = False
                    for apcr_item in settings.get("apcrs", []):
                        if apcr_item.get("camid") == camid:
                            cam_exists = True
                            break
                    
                    if cam_exists:
                        # Update the selected_camid setting
                        settings["global_settings"]["selected_camid"] = camid
                        save_settings(settings)
                        
                        # Update the active APC-R for the current event loop
                        apcr = get_active_apcr(settings)
                        if apcr:
                            print(f"Selected camid {camid}. Now controlling: {apcr['name']} ({apcr['ip']})")
                        else:
                            print(f"Selected camid {camid}, but couldn't get active APC-R details.")
                    else:
                        print(f"Error: No APC-R found with CamID {camid}")
                else:
                    print("Invalid CAM command format. Use e.g. cam1")

        clock.tick(30)

    # Make sure we stop the input thread and eat any remaining input before returning
    stop_event.set()
    print("Event loop terminated. Press Enter twice to continue...")
    try:
        input()
    except EOFError:
        pass


def flush_stdin():
    """
    Leeg de standaardinvoer (stdin) door eventuele restlijnen eruit te lezen.
    """
    import sys, select, msvcrt
    try:
        while select.select([sys.stdin], [], [], 0)[0]:
            sys.stdin.readline()
    except Exception:
        while msvcrt.kbhit():
            msvcrt.getch()



def find_action_for_button(device_mappings, btn_idx):
    for action_key, action_name in MAPPING_ACTIONS.items():
        binding = device_mappings.get(action_key)
        if not binding:
            continue
        if isinstance(binding, list):
            for m in binding:
                if m.get("type") == "button" and m.get("index") == btn_idx:
                    return action_key
        elif isinstance(binding, dict):
            if binding.get("type") == "button" and binding.get("index") == btn_idx:
                return action_key
    return None


def find_action_for_axis(device_mappings, axis_idx):
    for action_key, action_name in MAPPING_ACTIONS.items():
        binding = device_mappings.get(action_key)
        if not binding:
            continue
        if isinstance(binding, list):
            for m in binding:
                if m.get("type") == "axis" and m.get("index") == axis_idx:
                    return action_key
        elif isinstance(binding, dict):
            if binding.get("type") == "axis" and binding.get("index") == axis_idx:
                return action_key
    return None


def map_action_to_as_and_dir(map_name, trigger_type='axis'):
    as_name = None
    direction = None

    map_name_lower = map_name.lower()

    if 'pan_left' in map_name_lower:
        as_name = 'pan'
        if trigger_type == 'button':
            direction = 'negative'
    elif 'pan_right' in map_name_lower:
        as_name = 'pan'
        if trigger_type == 'button':
            direction = 'positive'
    elif 'tilt_up' in map_name_lower:
        as_name = 'tilt'
        if trigger_type == 'button':
            direction = 'negative'
    elif 'tilt_down' in map_name_lower:
        as_name = 'tilt'
        if trigger_type == 'button':
            direction = 'positive'
    elif 'roll_left' in map_name_lower:
        as_name = 'roll'
        if trigger_type == 'button':
            direction = 'negative'
    elif 'roll_right' in map_name_lower:
        as_name = 'roll'
        if trigger_type == 'button':
            direction = 'positive'
    elif 'zoom_in' in map_name_lower:
        as_name = 'zoom'
        if trigger_type == 'button':
            direction = 'in'
    elif 'zoom_out' in map_name_lower:
        as_name = 'zoom'
        if trigger_type == 'button':
            direction = 'out'

    if trigger_type == 'axis':
        direction = None

    debug_print(f"[DEBUG] Mapped action '{map_name}' to as_name='{as_name}', direction='{direction}' (trigger_type='{trigger_type}')")
    return as_name, direction


def call_non_continuous_action(map_name, apcr, settings):
    if map_name == "map_recenter":
        controls.send_recenter(apcr, send_apcr_command)
    elif map_name == "map_active_track":
        controls.toggle_active_track(apcr, settings, save_settings, send_apcr_command)
    elif map_name == "map_pan_tilt_speed_increase":
        controls.ptr_speed_increase(apcr, settings, save_settings)
    elif map_name == "map_pan_tilt_speed_decrease":
        controls.ptr_speed_decrease(apcr, settings, save_settings)
    elif map_name == "map_zoom_speed_increase":
        controls.zoom_speed_increase(apcr, settings, save_settings)
    elif map_name == "map_zoom_speed_decrease":
        controls.zoom_speed_decrease(apcr, settings, save_settings)
    elif map_name == "map_map_pan_tilt_speed_shortcut":
        controls.handle_pan_tilt_speed_shortcut(apcr, settings, save_settings)
    elif map_name == "map_zoom_speed_shortcut":
        controls.handle_zoom_speed_shortcut(apcr, settings, save_settings)
    elif map_name == "map_adaptive_speed_toggle":
        controls.toggle_adaptive_speed(apcr, settings, save_settings)    


def handle_apcr_settings(settings, apcr):
    while True:
        try:
            print("\nAPC-R settings:")
            print(f"1. Adjust zoom speed (current: {settings['global_settings'].get('zoom_speed',100)})")
            print(f"2. Adjust pan/tilt/roll speed (current: {settings['global_settings'].get('ptr_speed',100)})")
            print(f"3. Adjust position request frequency (current: {settings['global_settings'].get('position_request_frequency',1.2)} seconds)")
            print(f"4. Adjust status request frequency (current: {settings['global_settings'].get('status_request_frequency',1.0)} seconds)")
            print("5. Calibrate focus motor")
            print("6. Autotune Ronin")
            print(f"7. Preset transition speed (current: {settings['global_settings'].get('preset_transition_speed',100)})")
            print("8. Configure adaptive speed mapping")  # New option
            print("9. Return")
            c = input("> ").strip().lower()
            if c == "1":
                adjust_value(settings["global_settings"], "zoom_speed", "Zoom speed")
                save_settings(settings)
            elif c == "2":
                adjust_value(settings["global_settings"], "ptr_speed", "Pan/tilt/roll speed")
                save_settings(settings)
            elif c == "3":
                adjust_frequency(settings["global_settings"], "position_request_frequency", "Position request frequency (seconds)")
                save_settings(settings)
            elif c == "4":
                adjust_frequency(settings["global_settings"], "status_request_frequency", "Status request frequency (seconds)")
                save_settings(settings)
            elif c == "5":
                calibrate_focus(apcr)
            elif c == "6":
                autotune_ronin(apcr)
            elif c == "7":
                adjust_value(settings["global_settings"], "preset_transition_speed", "Preset transition speed")
                save_settings(settings)
            elif c == "8":
                configure_adaptive_speed_mapping(settings, apcr)
                save_settings(settings)
            elif c == "9" or c == "return":
                return
            else:
                print("Invalid choice, try again.")
        except Exception as e:
            print(f"[ERROR] Failed to handle APC-R settings: {e}")

def calibrate_focus(apcr):
    try:
        camid = int(apcr['camid'])
        camid_hex = f"{camid:02x}"
        data = bytes.fromhex("09" + camid_hex + "0400000e0f000000")
        send_apcr_command(apcr, data)
        print("Focus motor calibration command sent.")
    except Exception as e:
        print(f"[ERROR] Failed to calibrate focus motor: {e}")


def autotune_ronin(apcr):
    try:
        camid = int(apcr['camid'])
        camid_hex = f"{camid:02x}"
        data = bytes.fromhex("09" + camid_hex + "0400000e0e000000")
        send_apcr_command(apcr, data)
        print("Autotune Ronin command sent.")
    except Exception as e:
        print(f"[ERROR] Failed to autotune Ronin: {e}")


def adjust_value(settings_section, key, prompt):
    try:
        while True:
            val = input(f"{prompt} (current {settings_section[key]}) (1-100): ").strip()
            if val.isdigit():
                v = int(val)
                if 1 <= v <= 100:
                    settings_section[key] = v
                    print(f"{key.replace('_',' ')} set to {v}")
                    return
            print("Invalid value, try again.")
    except Exception as e:
        print(f"[ERROR] Failed to adjust value: {e}")


def adjust_frequency(settings_section, key, prompt):
    try:
        while True:
            val = input(f"{prompt} (current {settings_section[key]}) (0.1 - 10.0 seconds): ").strip()
            try:
                v = float(val)
                if 0.1 <= v <= 10.0:
                    settings_section[key] = v
                    print(f"{key.replace('_',' ')} set to {v} seconds")
                    return
            except ValueError:
                pass
            print("Invalid value, try again.")
    except Exception as e:
        print(f"[ERROR] Failed to adjust frequency: {e}")


def handle_settings(settings):
    while True:
        try:
            print("\nChange settings for APC-R:")
            apcrs = settings.get("apcrs", [])
            idx = 1
            for a in apcrs:
                print(f"{idx}. {a['name']} ({a['ip']})")
                idx += 1
            print(f"{idx}. Add APC-R connection manually")
            idx_add = idx
            idx += 1
            print(f"{idx}. Auto-discover APC-R devices")  # <- New option
            idx_discover = idx
            idx += 1
            print(f"{idx}. Remove APC-R Connection")
            idx_remove = idx
            idx += 1
            print(f"{idx}. Listener IP")
            idx_listener = idx
            idx += 1
            print(f"{idx}. TCP Settings for Bitfocus Companion")
            idx_tcp = idx
            idx += 1
            print(f"{idx}. Return")

            choice = input("> ").strip().lower()
            if choice == "return" or (choice.isdigit() and int(choice) == idx):
                return
            if choice.isdigit():
                c = int(choice)
                apcr_count = len(apcrs)
                if 1 <= c <= apcr_count:
                    apcr = apcrs[c-1]
                    handle_apcr_settings(settings, apcr)
                elif c == idx_add:
                    add_apcr_connection(settings)
                elif c == idx_discover:  # <- Handler for new auto-discovery option
                    discovered = discover_apcrs(settings)
                    if not discovered:
                        print("No new APC-R devices discovered.")
                    else:
                        print(f"Successfully discovered/updated {len(discovered)} APC-R devices:")
                        for device in discovered:
                            print(f"  - {device}")
                elif c == idx_remove:
                    remove_apcr_connection(settings)
                elif c == idx_listener:
                    set_listener_ip(settings)
                elif c == idx_tcp:
                    handle_tcp_settings(settings)
                else:
                    print("Invalid choice.")
            else:
                print("Invalid choice.")
        except Exception as e:
            print(f"[ERROR] Failed to handle settings: {e}")

def handle_mapping(settings):
    """
    Handle the mapping of device inputs to actions.
    
    Args:
        settings: Application settings
        
    Returns:
        None
    """
    try:
        device = choose_device()
        if device is None:
            return

        if hasattr(device, 'get_guid') and callable(device.get_guid):
            device_id = device.get_guid()
        else:
            device_id = device.get_name()
            
        # Ensure the device entry exists in settings
        if "devices" not in settings:
            settings["devices"] = {}
            
        if device_id not in settings["devices"]:
            settings["devices"][device_id] = {"name": device.get_name()}

        while True:
            action = choose_mapping_action(settings, device_id)
            if action is None:
                break
            if action == "camid_menu":
                map_camid_menu(settings, device)  # <-- nieuwe functie
                continue
                
            # Make sure action is a string if it's not already
            if isinstance(action, int):
                action = str(action)
                
            # Map the device action
            success = map_device_action(settings, device, action)
            
            # Verify the mapping was saved correctly
            if success:
                print(f"Verifying mapping saved correctly...")
                # Check if the action exists in the device mappings
                if device_id in settings["devices"]:
                    if action in settings["devices"][device_id]:
                        print(f"✓ Mapping for {action} found in settings.")
                    else:
                        print(f"[WARNING] Mapping for {action} not found in settings.")
                else:
                    print(f"[WARNING] Device {device_id} not found in settings.")
    except Exception as e:
        print(f"[ERROR] Failed to handle mapping: {e}")
        import traceback
        traceback.print_exc()


def map_camid_menu(settings, device):
    """
    Submenu om CamID‑selectie aan een knop/axis te binden.
    """
    # Verzamel en sorteer APC‑R’s
    apcrs = sorted(settings.get("apcrs", []), key=lambda a: a.get("camid", 0))
    if not apcrs:
        print("No APC‑Rs configured.")
        return

    # Device‑id ophalen
    device_id = device.get_guid() if hasattr(device, "get_guid") else device.get_name()
    dev_map = settings["devices"].setdefault(device_id, {"name": device.get_name()})

    while True:
        print("\nMap CamID to button:")
        for idx, apcr in enumerate(apcrs, start=1):
            camid = apcr.get("camid")
            key   = f"camid_{camid}"
            mapped = ""
            if key in dev_map:
                m = dev_map[key]
                mapped = f"[{m['type']} {m['index']}]"
            print(f"{idx}. {apcr['name']} – camID {camid} {mapped}")
        print(f"{len(apcrs)+1}. Return")

        choice = input("> ").strip().lower()
        if choice in ("return", str(len(apcrs)+1)):
            return
        if not choice.isdigit() or not (1 <= int(choice) <= len(apcrs)):
            print("Invalid choice.")
            continue

        sel_apcr = apcrs[int(choice)-1]
        camid_key = f"camid_{sel_apcr['camid']}"
        # --- wacht op knop/axis zoals bij wait_for_mapping_input ---
        chosen = wait_for_mapping_input(device)
        if chosen is None:
            print("Mapping cancelled.")
            continue

        # bestaande binding voor die knop/axis eerst weggooien
        remove_existing_bindings(dev_map, (chosen[0], chosen[1]))
        # opslaan
        dev_map[camid_key] = {
            "type":   chosen[0],
            "index":  chosen[1],
            **({"direction": chosen[2]} if chosen[0]=="axis" else {})
        }
        save_settings(settings)
        print(f"CamID {sel_apcr['camid']} mapped to {chosen[0]} {chosen[1]}.\n")


def choose_device():
    try:
        devices = list_devices()
        if not devices:
            print("No gamepads/joysticks found.")
            return None

        print("Detected the following input devices:")
        for i, d in enumerate(devices, start=1):
            print(f"{i}. {d.get_name()}")
        print("Type the number of the device to select it, or 'return' to go back.")

        while True:
            choice = input("> ").strip().lower()
            if choice == "return":
                return None
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(devices):
                    return devices[idx - 1]
            print("Invalid choice, try again.")
    except Exception as e:
        print(f"[ERROR] Failed to choose device: {e}")
        return None


def list_devices():
    try:
        count = pygame.joystick.get_count()
        devices = []
        for i in range(count):
            j = pygame.joystick.Joystick(i)
            j.init()
            devices.append(j)
        return devices
    except Exception as e:
        print(f"[ERROR] Failed to list devices: {e}")
        return []


def choose_mapping_action(settings, device_id):
    dev_map = settings["devices"].get(device_id, {})
    print("\nChoose a mapping action:")

    for k, v in MAPPING_ACTIONS.items():
        mapping_str = ""
        if k in dev_map:
            mapping = dev_map[k]
            if isinstance(mapping, list):
                mapping_str = ", ".join(
                    f"[{m['type']} {m['index']}" +
                    (f" {m['direction']}" if m['type'] == 'axis' else "") +
                    "]"
                    for m in mapping
                )
            else:
                if mapping["type"] == "button":
                    mapping_str = f"[button {mapping['index']}]"
                elif mapping["type"] == "axis":
                    mapping_str = f"[axis {mapping['index']} {mapping['direction']}]"
        print(f"{k}. {v} {mapping_str}")

    print("18. Map CamID to button")
    print("19. return")

    while True:
        choice = input("> ").strip().lower()
        if choice in ("19", "return"):
            return None
        if choice == "18":
            return "camid_menu"           # speciaal signaal
        if choice in MAPPING_ACTIONS:
            return choice
        print("Invalid choice, try again.")


def wait_for_mapping_input(joystick):
    print("\nMove the joystick or press a button to map this action.")
    print("Once an input is detected, press ENTER to save, or type 'cancel' then ENTER to abort.")
    print("Move another button/axis to change the selection before pressing ENTER.")

    last_axis_values = [0.0] * joystick.get_numaxes()
    chosen_input = None

    def print_selected_input(ci):
        if ci[0] == "button":
            print(f"Selected input: button {ci[1]}. Press ENTER to save or type 'cancel' then ENTER to abort.")
        else:
            print(f"Selected input: axis {ci[1]} ({ci[2]}). Press ENTER to save or type 'cancel' then ENTER to abort.")

    clock = pygame.time.Clock()
    input_buffer = ""

    while True:
        pygame.event.pump()

        # Check axes
        for axis_idx in range(joystick.get_numaxes()):
            value = joystick.get_axis(axis_idx)
            if abs(value - last_axis_values[axis_idx]) > 0.01:
                last_axis_values[axis_idx] = value
                if abs(value) > 0.3:
                    direction = "positive" if value > 0 else "negative"
                    new_input = ("axis", axis_idx, direction)
                    if new_input != chosen_input:
                        chosen_input = new_input
                        print_selected_input(chosen_input)

        # Check buttons
        for btn_idx in range(joystick.get_numbuttons()):
            if joystick.get_button(btn_idx):
                new_input = ("button", btn_idx)
                if new_input != chosen_input:
                    chosen_input = new_input
                    print_selected_input(chosen_input)

        # Non-blocking keyboard input
        while msvcrt.kbhit():
            char = msvcrt.getwche()
            if char == '\r':  # Enter pressed
                line = input_buffer.strip().lower()
                input_buffer = ""
                if chosen_input is None:
                    continue
                if line == "":
                    return chosen_input
                elif line == "cancel":
                    return None
                else:
                    print("Invalid choice.")
                    print_selected_input(chosen_input)
            elif char == '\b':
                if len(input_buffer) > 0:
                    input_buffer = input_buffer[:-1]
                    print("\b \b", end='', flush=True)
            elif char in ('\n', '\r'):
                pass
            else:
                input_buffer += char

        clock.tick(30)


def map_device_action(settings, device, action):
    """
    Map a device action to an input.
   
    Args:
        settings: Application settings
        device: The input device (joystick)
        action: The action key to map
       
    Returns:
        bool: True if successful, False otherwise
    """
    chosen_input = wait_for_mapping_input(device)
    if chosen_input is None:
        print("Mapping canceled.")
        return False

    # Get device ID (either from guid or name)
    if hasattr(device, 'get_guid') and callable(device.get_guid):
        device_id = device.get_guid()
    else:
        device_id = device.get_name()

    # Ensure the device exists in settings
    if "devices" not in settings:
        settings["devices"] = {}
       
    if device_id not in settings["devices"]:
        settings["devices"][device_id] = {"name": device.get_name()}

    current_mappings = settings["devices"][device_id]
    map_name = MAPPING_ACTIONS[action]

    # Check if this action allows multiple mappings (like pan, tilt, zoom)
    allows_multiple = any(x in map_name for x in ["pan_", "tilt_", "zoom_"])

    # Create the new mapping
    new_mapping = {
        "type": chosen_input[0],
        "index": chosen_input[1]
    }
    if chosen_input[0] == "axis":
        new_mapping["direction"] = chosen_input[2]
        # Remove existing bindings for this specific axis+direction combination
        remove_existing_bindings(current_mappings, ("axis", new_mapping["index"]), new_mapping["direction"])
    else:
        # Remove existing bindings for this button
        remove_existing_bindings(current_mappings, ("button", new_mapping["index"]))

    # Add the new mapping
    if allows_multiple:
        if action not in current_mappings or not isinstance(current_mappings[action], list):
            current_mappings[action] = []
        if any(
            m["type"] == new_mapping["type"] and m["index"] == new_mapping["index"] and
            m.get("direction") == new_mapping.get("direction")
            for m in current_mappings[action]
        ):
            print(f"This input is already mapped for '{map_name}'.")
        else:
            current_mappings[action].append(new_mapping)
            print(f"Added new mapping for '{map_name}': {new_mapping}")
    else:
        current_mappings[action] = new_mapping
        print(f"Set mapping for '{map_name}' to: {new_mapping}")

    # Make sure action is a string key in the mappings
    if isinstance(action, int):
        str_action = str(action)
        current_mappings[str_action] = current_mappings.pop(action)
        print(f"Converted mapping key from {action} to '{str_action}'")
   
    # Save settings to file
    result = save_settings(settings)
   
    if result:
        print("Mapping saved to settings.json.")
    else:
        print("[ERROR] Failed to save mapping to settings.json.")
   
    return result



def remove_existing_bindings(device_mappings, input_trigger, direction=None):
    """
    Remove existing bindings for a specific input (button or axis) from device mappings.
    
    Args:
        device_mappings: Dictionary of mappings for the device
        input_trigger: Tuple (input_type, input_idx) specifying which input to remove
        direction: Optional direction ("positive" or "negative") for axis inputs
        
    Returns:
        None
    """
    input_type, input_idx = input_trigger
    actions_to_remove = []
    
    for action, bindings in device_mappings.items():
        # Skip non-mapping entries like "name"
        if action == "name":
            continue
            
        if isinstance(bindings, list):
            # For actions with multiple bindings (list)
            new_bindings = []
            for b in bindings:
                # For axis inputs with direction specified, only remove matching direction
                if input_type == "axis" and direction and b.get("type") == "axis" and b.get("index") == input_idx:
                    # Keep if directions don't match
                    if b.get("direction") != direction:
                        new_bindings.append(b)
                # For other inputs, remove if type and index match
                elif not (input_type == "axis" and direction) and b.get("type") == input_type and b.get("index") == input_idx:
                    # Remove
                    pass
                else:
                    # Keep
                    new_bindings.append(b)
            
            if new_bindings:
                device_mappings[action] = new_bindings
            else:
                actions_to_remove.append(action)
        elif isinstance(bindings, dict):
            # For actions with a single binding (dict)
            if input_type == "axis" and direction:
                # Only remove if both type, index and direction match
                if bindings.get("type") == "axis" and bindings.get("index") == input_idx and bindings.get("direction") == direction:
                    actions_to_remove.append(action)
            elif bindings.get("type") == input_type and bindings.get("index") == input_idx:
                # For non-axis or when direction doesn't matter
                actions_to_remove.append(action)

    # Remove actions that no longer have bindings
    for action in actions_to_remove:
        del device_mappings[action]

def send_status_requests(settings):
    """
    Periodically send status requests to all APC-R devices.
    This can be done via broadcast or individual requests.
    """
    while True:
        try:
            # Try to use broadcast first if possible
            broadcast_success = False
            listener_ip = settings.get("listener_ip", "0.0.0.0")
            
            # If we have a specific listener IP (not 0.0.0.0), 
            # calculate the broadcast address
            if listener_ip != "0.0.0.0":
                # Convert IP to broadcast by setting last octet to 255
                ip_parts = listener_ip.split('.')
                if len(ip_parts) == 4:
                    broadcast_ip = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}.255"
                    
                    # Create a temporary socket that allows broadcasting
                    temp_socket = None
                    try:
                        temp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                        temp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                        temp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                        temp_socket.settimeout(1)
                        
                        # OKAPCR status request packet
                        data = bytes.fromhex("6f6b41504352")
                        
                        # Send the packet to the broadcast address, port 2390
                        temp_socket.sendto(data, (broadcast_ip, 2390))
                        if debug_mode:
                            print(f"[DEBUG] Broadcast status request sent to {broadcast_ip}:2390")
                        broadcast_success = True
                    except Exception as e:
                        print(f"[ERROR] Broadcast status request failed: {e}")
                    finally:
                        if temp_socket:
                            temp_socket.close()
            
            # If broadcast failed or wasn't possible, send individual requests
            if not broadcast_success:
                apcrs = settings.get("apcrs", [])
                for apcr in apcrs:
                    data = bytes.fromhex("6f6b41504352")
                    send_apcr_command(apcr, data)
                    if debug_mode:
                        print(f"[DEBUG] Individual status request sent to {apcr['name']} (CamID={apcr.get('camid')}, IP={apcr['ip']})")
            
            # Wait according to the configured frequency
            frequency = settings["global_settings"].get("status_request_frequency", 1.0)
            time.sleep(frequency)
        except Exception as e:
            print(f"[ERROR] Error in status request loop: {e}")
            time.sleep(1.0)  # Wait after error to avoid tight loop


def main_menu(settings):
    # Probeer eerst de stdin-buffer leeg te maken
    flush_stdin()
    while True:
        try:
            print("\nType 'map', 'settings', 'presets', 'start', 'help', or 'quit'")
            cmd = input("> ").strip().lower()
            if cmd == "":
                continue
            if cmd == "help":
                show_main_help()
            elif cmd == "settings":
                handle_settings(settings)
            elif cmd == "start":
                event_loop(settings)
            elif cmd == "map":
                handle_mapping(settings)
            elif cmd == "presets":
                handle_presets_menu(settings)
            elif cmd.startswith("cam"):
                camid_str = cmd[3:]
                if camid_str.isdigit():
                    camid = int(camid_str)
                    settings["global_settings"]["selected_camid"] = camid
                    save_settings(settings)
                    print(f"Selected camid {camid}.")
                else:
                    print("Invalid CAM command format. Use e.g. cam1")
            elif cmd in ("quit", "exit"):
                print("Exiting...")
                break
            else:
                print("Unknown command. Type 'help' for a list of commands.")
        except Exception as e:
            print(f"[ERROR] An error occurred in main menu loop: {e}")

def send_position_requests(settings):
    """
    (Re)send position requests.
    – Tot álle APC‑R’s geïnitialiseerd zijn: poll álle camera’s.
    – Daarna: alleen de geselecteerde CamID (zoals nu al gebeurde).
    """
    while True:
        try:
            apcrs = settings.get("apcrs", [])
            uninit_camids = [
                a['camid'] for a in apcrs
                if a.get('camid') not in _initialized_apcrs
            ]

            # -- 1) Startup‑fase: niet‑geïnitialiseerde camera’s eerst --
            if uninit_camids:
                for apcr in apcrs:
                    if apcr.get('camid') in uninit_camids:
                        _send_single_position_request(apcr)
                # kortere poll om sneller door de init‑fase te komen
                time.sleep(0.5)
                continue     # sla de “selected only”‑logica hieronder over

            # -- 2) Normale fase: alleen de geselecteerde camID --
            selected_camid = settings["global_settings"].get("selected_camid")
            if selected_camid is not None:
                for apcr in apcrs:
                    if apcr.get('camid') == selected_camid:
                        _send_single_position_request(apcr)
                        break

            freq = settings["global_settings"].get("position_request_frequency", 1.2)
            time.sleep(freq)

        except Exception as e:
            print(f"[ERROR] Error in position request loop: {e}")
            time.sleep(1.0)

def _send_single_position_request(apcr):
    camid_hex = f"{apcr['camid']:02x}"
    data = bytes.fromhex("08" + camid_hex + "0400000e140000")
    send_apcr_command(apcr, data)
    if debug_mode:
        print(f"[DEBUG] Position request sent to {apcr['name']} (CamID {apcr['camid']})")


def main():
    settings = load_settings()
    if settings is None:
        print("[ERROR] Failed to load settings. Exiting.")
        return

    # Initialize pygame for joystick handling
    init_pygame()
    
    # Initialize the global socket for sending
    if not init_global_socket(settings):
        print("[ERROR] Failed to initialize global socket. Exiting.")
        return
    
    # Start the central UDP listener
    if not controls.init_udp_listener(settings):
        print("[ERROR] Could not start UDP listener. Application will exit.")
        return
    
    # Start position requests and status requests
    pos_req_thread = threading.Thread(target=send_position_requests, args=(settings,), daemon=True)
    pos_req_thread.start()
    status_req_thread = threading.Thread(target=send_status_requests, args=(settings,), daemon=True)
    status_req_thread.start()

    # Start checking for new APC-Rs that connect
    check_thread = threading.Thread(target=check_for_new_apcrs, args=(settings,), daemon=True)
    check_thread.start()

    # Initialize debug_mode in settings
    global debug_mode
    settings["debug_mode"] = debug_mode  # Use the global debug_mode variable
    controls.set_debug_mode(debug_mode)  # Synchronize with controls module

    # Start the TCP server for Bitfocus Companion integration if enabled
    tcp_server = None
    tcp_thread = None
    if settings.get("enable_tcp_connection", False):
        try:
            import interpreter
            tcp_server, tcp_thread = interpreter.init_tcp_server(
                settings, 
                send_apcr_command=send_apcr_command, 
                get_active_apcr=get_active_apcr,
                save_settings_func=save_settings
            )
            
        except ImportError:
            print("[WARNING] Could not load interpreter module. TCP server will not be started.")
        except Exception as e:
            print(f"[WARNING] Error starting TCP server: {e}")
    
    # Start active track monitor
    controls.start_active_track_monitor(settings, send_apcr_command, save_settings)

    # Print summary of configuration
    print("\n=== Configuration Summary ===")
    apcrs = settings.get("apcrs", [])
    if len(apcrs) > 0:
        print(f"APC-R Cameras Configured: {len(apcrs)}")
        for idx, apcr in enumerate(apcrs, 1):
            selected = " (SELECTED)" if apcr.get('camid') == settings["global_settings"].get("selected_camid") else ""
            print(f"  {idx}. {apcr['name']} - CamID: {apcr.get('camid')}, IP: {apcr['ip']}{selected}")
    else:
        print("No APC-R cameras configured.")
    
    print(f"Position Request Frequency: {settings['global_settings'].get('position_request_frequency', 1.2)} seconds")
    print(f"Status Request Frequency: {settings['global_settings'].get('status_request_frequency', 5.0)} seconds")
    print(f"TCP Server for Companion: {'ENABLED' if settings.get('enable_tcp_connection', False) else 'DISABLED'}")
    print(f"Debug Mode: {'ON' if debug_mode else 'OFF'}")
    print("============================\n")

    # Start your command mode or main menu
    if len(apcrs) > 0:
        selected_camid = settings["global_settings"].get("selected_camid")
        if selected_camid is None:
            print("\nNo camera selected. Please select a camera with the 'cam[num]' command.")
            print("Available cameras:")
            for apcr in apcrs:
                print(f"  cam{apcr.get('camid')} - {apcr['name']} ({apcr['ip']})")
        else:
            print("\nStarting in command mode with selected camera.")
            print("Type 'debug' to toggle debug mode, 'cam[num]' to select CamID, 'help' for help, or 'quit' to exit.")
        
        event_loop(settings)
        main_menu(settings)
    else:
        print("\nNo APC-Rs configured. Starting in main menu.")
        main_menu(settings)
    
    # Stop the TCP server when exiting
    if tcp_server:
        try:
            import interpreter
            interpreter.stop_tcp_server(tcp_server, tcp_thread)
        except Exception as e:
            print(f"[ERROR] Error stopping TCP server: {e}")
            
    print("APCR Controller exiting. Goodbye!")




def handle_presets_menu(settings):
    try:
        print("\nMap Presets Menu:")
        apcrs = settings.get("apcrs", [])
        if not apcrs:
            print("No APC-Rs configured.")
            return

        print("Select the APC-R/CamID to manage presets:")
        for idx, apcr in enumerate(apcrs, start=1):
            print(f"{idx}. {apcr['name']} (CamID: {apcr.get('camid', 'N/A')})")
        print(f"{len(apcrs)+1}. Return")

        while True:
            choice = input("> ").strip().lower()
            if choice == "return" or (choice.isdigit() and int(choice) == len(apcrs)+1):
                return
            if choice.isdigit():
                idx = int(choice)
                if 1 <= idx <= len(apcrs):
                    selected_apcr = apcrs[idx-1]
                    manage_presets_for_apcr(settings, selected_apcr)
                    return
            print("Invalid choice, try again.")
    except Exception as e:
        print(f"[ERROR] Failed to handle presets menu: {e}")


def manage_presets_for_apcr(settings, apcr):
    cam_id = apcr.get("camid")
    if cam_id is None:
        print("[ERROR] CamID is not set for this APC-R.")
        return

    while True:
        print(f"\nManaging Presets for {apcr['name']} (CamID: {cam_id}):")
        print("1. Map a Preset to a Button")
        print("2. Delete All Preset Data")
        print("3. Return")

        choice = input("> ").strip().lower()
        if choice == "1":
            map_preset_to_button(settings, apcr)
        elif choice == "2":
            confirm = input("Are you sure you want to delete all presets? (yes/no): ").strip().lower()
            if confirm == "yes":
                presets.delete_all_presets()
                print("All presets have been deleted.")
                # Ook gemapte preset-entries uit settings verwijderen
                for device_id, device_map in settings["devices"].items():
                    keys_to_remove = [k for k in device_map if k.startswith(f"preset_{cam_id}.")]
                    for k in keys_to_remove:
                        del device_map[k]
                save_settings(settings)
        elif choice == "3" or choice == "return":
            return
        else:
            print("Invalid choice, try again.")


def map_preset_to_button(settings, apcr):
    cam_id = apcr.get("camid")
    if cam_id is None:
        print("[ERROR] CamID is not set for this APC-R.")
        return

    print("\nSelect the preset slot to map:")
    presets_list = presets.list_presets(cam_id)
    existing_slots = list(presets_list.keys())
    for idx, key in enumerate(existing_slots, start=1):
        data = presets_list[key]
        mapped_buttons = data.get("mapped_buttons", {})
        
        device_names = []
        for device_id, btn in mapped_buttons.items():
            device_name = get_device_name_from_id(settings, device_id)
            device_names.append(f"{device_name}: button{btn}")
        
        mapped_buttons_str = ", ".join(device_names) if device_names else "None"
        position = data.get("position", "N/A")
        print(f"{idx}. {key}: {position} | {mapped_buttons_str}")
    print(f"{len(existing_slots)+1}. Create a new preset slot")
    print(f"{len(existing_slots)+2}. Return")

    while True:
        choice = input("> ").strip().lower()
        if choice == str(len(existing_slots)+1):
            slot_number = input("Enter the slot number to create (1-100): ").strip()
            if slot_number.isdigit():
                slot_number = int(slot_number)
                if 1 <= slot_number <= 100:
                    slot_key = f"{cam_id}.{slot_number}"
                    if slot_key in presets_list:
                        print("Preset slot already exists.")
                    else:
                        current_position = controls.get_current_position(apcr)
                        if current_position:
                            presets.save_preset(cam_id, slot_number, current_position)
                            print(f"Preset {slot_key} saved.")
                        else:
                            print("[ERROR] Current position data is not available.")
                else:
                    print("Invalid slot number. Must be between 1 and 100.")
            else:
                print("Invalid input. Please enter a number.")
        elif choice == str(len(existing_slots)+2) or choice == "return":
            return
        elif choice.isdigit() and 1 <= int(choice) <= len(existing_slots):
            selected_preset = existing_slots[int(choice)-1]
            map_preset_to_button_process_selection(settings, apcr, selected_preset)
            return
        else:
            print("Invalid choice, try again.")


def get_device_name_from_id(settings, device_id):
    devices = settings.get("devices", {})
    device_info = devices.get(device_id, {})
    return device_info.get("name", device_id)


def map_preset_to_button_process_selection(settings, apcr, selected_preset):
    cam_id = apcr.get("camid")
    if cam_id is None:
        print("[ERROR] CamID is not set for this APC-R.")
        return

    slot_number = int(selected_preset.split('.')[1])

    print("\nSelect the device to map this preset to:")
    devices = list_devices()
    if not devices:
        print("No devices found.")
        return

    for idx, device in enumerate(devices, start=1):
        print(f"{idx}. {device.get_name()}")
    print(f"{len(devices)+1}. Return")

    while True:
        choice = input("> ").strip().lower()
        if choice == str(len(devices)+1) or choice == "return":
            return
        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(devices):
                selected_device = devices[idx-1]
                break
        print("Invalid choice, try again.")

    chosen_input = wait_for_preset_button_mapping(selected_device)
    if chosen_input is None:
        print("Mapping canceled.")
        return

    if hasattr(selected_device, 'get_guid') and callable(selected_device.get_guid):
        device_id = selected_device.get_guid()
    else:
        device_id = selected_device.get_name()

    if device_id not in settings["devices"]:
        settings["devices"][device_id] = {"name": selected_device.get_name()}

    # Verwijder bestaande bindings voor deze knop
    if chosen_input[0] == "button":
        remove_existing_bindings(settings["devices"][device_id], ("button", chosen_input[1]))
    elif chosen_input[0] == "axis":
        remove_existing_bindings(settings["devices"][device_id], ("axis", chosen_input[1]))

    preset_action = f"preset_{cam_id}.{slot_number}"

    new_mapping = {
        "type": chosen_input[0],
        "index": chosen_input[1]
    }
    if chosen_input[0] == "axis":
        new_mapping["direction"] = chosen_input[2]

    if preset_action in settings["devices"][device_id]:
        if isinstance(settings["devices"][device_id][preset_action], list):
            settings["devices"][device_id][preset_action].append(new_mapping)
        else:
            settings["devices"][device_id][preset_action] = [
                settings["devices"][device_id][preset_action], 
                new_mapping
            ]
    else:
        settings["devices"][device_id][preset_action] = new_mapping

    # Sla op in presets.json
    result = presets.save_preset(
        cam_id,
        slot_number,
        presets.get_preset(cam_id, slot_number)['position'],
        mapped_button=chosen_input[1],
        device_id=device_id
    )
    
    # Invalidate the button mapping cache
    if result:
        invalidate_button_mapping_cache()
    
    print(f"Preset {cam_id}.{slot_number} is now mapped to {chosen_input[0]} {chosen_input[1]} on Device '{selected_device.get_name()}'.")
    save_settings(settings)


def wait_for_preset_button_mapping(device):
    print("\nPress the button you want to map this preset to. Press ENTER to confirm or type 'cancel' then ENTER to abort.")

    chosen_button = None
    input_buffer = ""

    while True:
        pygame.event.pump()

        for btn_idx in range(device.get_numbuttons()):
            if device.get_button(btn_idx):
                if chosen_button != btn_idx:
                    chosen_button = btn_idx
                    print(f"Selected Button: {chosen_button}")
                time.sleep(0.2)  # rudimentaire debounce

        if msvcrt.kbhit():
            char = msvcrt.getwche()
            if char == '\r':
                line = input_buffer.strip().lower()
                if line == "cancel":
                    return None
                elif line == "":
                    return ("button", chosen_button)
                else:
                    print("Invalid input. Type 'cancel' to abort or press ENTER to confirm.")
                input_buffer = ""
            elif char == '\b':
                if len(input_buffer) > 0:
                    input_buffer = input_buffer[:-1]
                    print("\b \b", end='', flush=True)
            else:
                input_buffer += char

        time.sleep(0.05)

def handle_preset_transition_speed(settings):
    try:
        val = input("Enter new preset_transition_speed in seconds (float), e.g. 1.5: ").strip()
        new_val = float(val)
        if new_val < 0.1:
            print("[ERROR] Must be >=0.1 second.")
            return
        settings["global_settings"]["preset_transition_speed"] = new_val
        save_settings(settings)
        print(f"Preset transition speed set to {new_val} seconds.")
    except ValueError:
        print("[ERROR] Invalid float value.")

def handle_tcp_settings(settings):
    """Handle TCP server settings"""
    try:
        while True:
            print("\nTCP Settings for Bitfocus Companion:")
            print(f"1. TCP Connection {'Enabled' if settings.get('enable_tcp_connection', False) else 'Disabled'}")
            print(f"2. TCP Port: {settings.get('tcp_port', 11580)}")
            print(f"3. TCP Listener IP: {settings.get('tcp_listener_ip', '0.0.0.0')}")
            print("4. Return")
            
            choice = input("> ").strip().lower()
            
            if choice == "1":
                current = settings.get("enable_tcp_connection", False)
                settings["enable_tcp_connection"] = not current
                print(f"TCP Connection {'enabled' if not current else 'disabled'}")
                print("The application must be restarted to apply the changes.")
                save_settings(settings)
                
            elif choice == "2":
                try:
                    port = int(input(f"Enter TCP port (current: {settings.get('tcp_port', 11580)}): ").strip())
                    if 1024 <= port <= 65535:
                        settings["tcp_port"] = port
                        print(f"TCP port set to {port}")
                        save_settings(settings)
                        print("The application must be restarted to apply the changes.")
                    else:
                        print("Invalid port. Must be between 1024 and 65535.")
                except ValueError:
                    print("Invalid input. Please enter a number.")
                    
            elif choice == "3":
                ip = input(f"Enter TCP listener IP (current: {settings.get('tcp_listener_ip', '0.0.0.0')}): ").strip()
                
                if not ip:
                    ip = "0.0.0.0"
                
                settings["tcp_listener_ip"] = ip
                print(f"TCP listener IP set to {ip}")
                save_settings(settings)
                print("The application must be restarted to apply the changes.")
                    
            elif choice == "4" or choice == "return":
                return
            else:
                print("Invalid choice.")
                
    except Exception as e:
        print(f"[ERROR] Error managing TCP settings: {e}")


def configure_adaptive_speed_mapping(settings, apcr):
    """
    Configure adaptive speed mapping for different zoom levels for a specific APC-R.
    
    Args:
        settings: Application settings dictionary
        apcr: The APC-R configuration to modify
    """
    while True:
        print(f"\nAdaptive Speed Mapping Configuration for {apcr['name']} (CamID {apcr.get('camid')})")
        print("This determines how PTR speed is adjusted based on zoom level for this specific camera.")
        
        # Show current mapping values
        print("\nCurrent mapping:")
        print(f"- 0% zoom -> {apcr.get('adaptive_speed_map_0', 50)}% speed")
        print(f"- 10% zoom -> {apcr.get('adaptive_speed_map_10', 'Not set')}% speed")
        print(f"- 25% zoom -> {apcr.get('adaptive_speed_map_25', 'Not set')}% speed")
        print(f"- 50% zoom -> {apcr.get('adaptive_speed_map_50', 'Not set')}% speed")
        print(f"- 75% zoom -> {apcr.get('adaptive_speed_map_75', 'Not set')}% speed")
        print(f"- 100% zoom -> {apcr.get('adaptive_speed_map_100', 3)}% speed")
        
        print("\nSelect zoom level to configure:")
        print("1. 0% zoom level")
        print("2. 10% zoom level")
        print("3. 25% zoom level")
        print("4. 50% zoom level")
        print("5. 75% zoom level")
        print("6. 100% zoom level")
        print("7. Reset to defaults")
        print("8. Return")
        
        choice = input("> ").strip().lower()
        
        if choice == "1":
            adjust_adaptive_speed_value(apcr, "adaptive_speed_map_0", "0% zoom level speed", 50)
        elif choice == "2":
            adjust_adaptive_speed_value(apcr, "adaptive_speed_map_10", "10% zoom level speed")
        elif choice == "3":
            adjust_adaptive_speed_value(apcr, "adaptive_speed_map_25", "25% zoom level speed")
        elif choice == "4":
            adjust_adaptive_speed_value(apcr, "adaptive_speed_map_50", "50% zoom level speed")
        elif choice == "5":
            adjust_adaptive_speed_value(apcr, "adaptive_speed_map_75", "75% zoom level speed")
        elif choice == "6":
            adjust_adaptive_speed_value(apcr, "adaptive_speed_map_100", "100% zoom level speed", 3)
        elif choice == "7":
            # Reset to defaults
            if "adaptive_speed_map_0" in apcr:
                apcr["adaptive_speed_map_0"] = 50
            if "adaptive_speed_map_10" in apcr:
                del apcr["adaptive_speed_map_10"]
            if "adaptive_speed_map_25" in apcr:
                del apcr["adaptive_speed_map_25"]
            if "adaptive_speed_map_50" in apcr:
                del apcr["adaptive_speed_map_50"]
            if "adaptive_speed_map_75" in apcr:
                del apcr["adaptive_speed_map_75"]
            if "adaptive_speed_map_100" in apcr:
                apcr["adaptive_speed_map_100"] = 3
            
            save_settings(settings)
            print("Reset adaptive speed mapping to defaults.")
        elif choice == "8" or choice == "return":
            return
        else:
            print("Invalid choice. Please try again.")

def adjust_adaptive_speed_value(apcr_config, key, prompt, default_value=None):
    """
    Adjust an adaptive speed mapping value, with option to clear it.
    
    Args:
        apcr_config: The APC-R configuration to modify
        key: The key to adjust
        prompt: The prompt to display
        default_value: Default value if not already set
    """
    current = apcr_config.get(key, default_value)
    current_display = current if current is not None else "Not set"
    
    print(f"\nCurrent {prompt}: {current_display}")
    print("Enter a new value (1-100), empty to clear, or 'cancel' to cancel:")
    
    value = input("> ").strip()
    
    if value.lower() == "cancel":
        return
    elif value == "":
        # Clear the value if it exists
        if key in apcr_config:
            del apcr_config[key]
        print(f"{prompt} cleared. Will use interpolated value.")
    elif value.isdigit():
        val = int(value)
        if 1 <= val <= 100:
            apcr_config[key] = val
            print(f"{prompt} set to {val}%")
        else:
            print("Value must be between 1 and 100.")
    else:
        print("Invalid input. Value not changed.")

def show_help():
    print("\nHelp - Available Commands in Command Mode:")
    print("  debug      - Toggle debug mode on or off.")
    print("  cam[num]   - Select the CamID number to switch between different APC-Rs (e.g., cam1, cam2).")
    print("  save[x.y]  - Save the current position to preset slot y for CamID x (e.g., save1.1).")
    print("  virtualwall on/off  - enable/disable virtual wall for manual control.")
    print("  virtualwallpreset on/off  - enable/disable virtual wall for presets.") 
    print("  virtualwalltiltclear - delete tilt virtual wall.") 
    print("  virtualwallpanclear - delete pan virtual wall.") 
    print("  virtualwallpanstart  - set current pan position as the start of the virtual wall border.")
    print("  virtualwallpansend  - set current tilt position as the end of the virtual wall border.")
    print("  virtualwalltiltstart  - set current tilt position as the start of the virtual wall border.")
    print("  virtualwalltiltstart  - set current tilt position as the end of the virtual wall border.")
    print("  adaptivespeed on/off - Enable or disable speed adjustment based on zoom level.")
    print("  transitionspeed - set transition speed precentage."),
    print("  help       - Show this help message.")
    print("  settings   - Return to the main menu to adjust settings.")
    print("  quit/exit  - Exit the program and go to main menu.")
    


def show_main_help():
    print("\nHelp - Available Commands in Main Menu:")
    print("  map       - Map device actions to inputs.")
    print("  settings  - Adjust APC-R settings.")
    print("  presets   - Manage camera presets.")
    print("  start     - Start the program to control the APC-R.")
    print("  help      - Show this help message.")
    print("  quit      - Exit the program.")



if __name__ == "__main__":
    main()


